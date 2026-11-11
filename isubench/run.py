import datetime
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import agents, aws, benchd, hosts, prices
from .problems import LANGS, PROBLEMS

ROOT = Path(__file__).resolve().parent.parent
IMAGE = 'isu-bench-agent:v1'
RESUMES = 3
TTL_HOURS = 48
SESSION_DIRS = ('.claude/projects', '.codex/sessions', '.dsh/sessions', '.agy')
# dsh compresses its session logs with zstd; Node in the agent image can read them, Python 3.12 cannot.
UNZSTD = ("const fs=require('fs'),zlib=require('zlib'),path=require('path');const walk=d=>{for(const n of "
          "fs.readdirSync(d)){const p=path.join(d,n);if(fs.statSync(p).isDirectory())walk(p);else if"
          "(p.endsWith('.jsonl.zstd'))fs.writeFileSync(p.slice(0,-5),zlib.zstdDecompressSync(fs.readFileSync(p)))}};"
          "walk('/home/agent/.dsh/sessions')")


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def prepare_workspace(dest: Path, problem):
    cache = ROOT / 'runs' / '.upstream' / problem.name
    if not cache.exists():
        sh('git', 'clone', '-q', '--filter=blob:none', '--no-checkout', problem.upstream, str(cache))
    sh('git', '-C', str(cache), 'checkout', '-q', problem.commit)
    for d in ('docs', 'webapp'):
        shutil.copytree(cache / d, dest / d)
    return commit_workspace(dest)


def commit_workspace(dest: Path):
    sh('git', 'init', '-q', str(dest))
    sh('git', '-C', str(dest), 'add', '-A')
    sh('git', '-C', str(dest), '-c', 'user.name=isu-bench', '-c', 'user.email=isu-bench@localhost', 'commit', '-qm', 'initial')
    return sh('git', '-C', str(dest), 'rev-parse', 'HEAD', capture_output=True).stdout.strip()


def render_prompt(problem, lang):
    fields = {'{TITLE}': problem.title, '{SERVICE}': problem.service, '{BENCH_NOTE}': problem.bench_note,
              '{LANG}': LANGS[lang], '{lang}': lang, '{RUNS}': str(benchd.LIMIT)}
    text = (ROOT / 'prompt.md').read_text()
    for key, value in fields.items():
        text = text.replace(key, value)
    return text


def keygen(path: Path, comment):
    sh('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', comment, '-f', str(path))
    return path.with_suffix('.pub').read_text().strip()


def scrub(directory: Path, secrets):
    for f in directory.rglob('*'):
        if f.is_file():
            text = f.read_text(errors='surrogateescape')
            if any(s in text for s in secrets):
                for s in secrets:
                    text = text.replace(s, '[REDACTED]')
                f.write_text(text, errors='surrogateescape')


def close_ssh(problem, infra, h, work, agent, result):
    flag = work / 'proxy' / 'ssh-closed'
    flag.touch()
    log = work / 'proxy' / 'proxy.jsonl'
    deadline = time.monotonic() + 30
    while not (log.exists() and '"event": "ssh-closed"' in log.read_text()):
        if time.monotonic() > deadline:
            return 'the proxy did not close the SSH tunnels'
        time.sleep(1)
    if not infra.state.get('client_ssh_closed'):
        infra.close_client_ssh(problem)
        infra.save('client_ssh_closed', True)
    reasons = [f'{host} still has SSH sessions from {peers}' for host, peers in problem.close_sessions(h).items()]
    probe = subprocess.run(['docker', 'exec', agent, 'nc', '-X', 'connect', '-x', 'proxy:3128', '-w', '5',
                            h.hosts[problem.apps[0]]['public'], '22'], capture_output=True, stdin=subprocess.DEVNULL)
    if probe.returncode == 0:
        reasons.append('the agent still reaches SSH through the proxy')
    if problem.audit_network:
        diffs = infra.audit(problem, ssh_closed=True)
        result.setdefault('aws_checks', []).append({'stage': 'ssh closed', 'differences': diffs})
        reasons += diffs
    return '; '.join(reasons) or None


def runner_for(problem, h, infra, out, limit=benchd.LIMIT, before=None):
    return benchd.Runner(out, ['ssh', '-F', str(h.config), '-o', 'BatchMode=yes', 'bench'],
                         lambda attempt, runs: problem.run_command(h.hosts, attempt, runs), problem.outcome,
                         lambda: h.recover_bench(infra), lambda: problem.recover(h, infra),
                         before=before or (lambda n, attempt: problem.before_run(h, n, attempt)),
                         after=lambda n, attempt, outcome: problem.after_run(h, n, attempt, outcome),
                         limit=limit, infra_limit=problem.infra_limit)


def audit(problem, infra, result, stage):
    if problem.audit_network:
        diffs = infra.audit(problem, ssh_closed=bool(infra.state.get('client_ssh_closed')))
        result.setdefault('aws_checks', []).append({'stage': stage, 'differences': diffs})
        if diffs and stage == 'provisioned':
            raise RuntimeError(f'the network does not match the problem: {diffs}')


def run(problem_name, agent_name, model, effort, lang, keep=False):
    problem = PROBLEMS[problem_name]
    if lang not in problem.langs:
        raise SystemExit(f'{problem.name} has no {lang} implementation (available: {", ".join(problem.langs)})')
    adapter = agents.AGENTS[agent_name]
    credentials = [c for c in adapter.credentials if os.environ.get(c)]
    auth_store = getattr(adapter, 'auth_store', None)
    upstream = getattr(adapter, 'upstream', None)
    if credentials or not (upstream and os.environ.get(upstream['credential'])):
        upstream = None
    if not credentials and not upstream and not (auth_store and auth_store.exists()):
        raise SystemExit(f'set one of {", ".join(adapter.credentials)}' + (f' or create {auth_store}' if auth_store else '')
                         + (f" or {adapter.upstream['credential']}" if hasattr(adapter, 'upstream') else ''))
    secrets = [os.environ[c] for c in credentials] + ([os.environ[upstream['credential']]] if upstream else [])
    started = now()
    run_id = re.sub(r'[^a-z0-9-]+', '-', f'{problem.name}-{agent_name}-{model}-{lang}-{started:%Y%m%d%H%M%S}'.lower())
    work, res = ROOT / 'runs' / run_id, ROOT / 'results' / run_id
    home, ws, sock = work / 'home', work / 'workspace', work / 'sock'
    for d in (home / '.ssh', ws, sock, work / 'proxy', res / 'bench', res / 'agent'):
        d.mkdir(parents=True, exist_ok=True)
    print(f'run {run_id}', flush=True)

    sh('docker', 'build', '-q', '-t', IMAGE, '--build-arg', f'UID={os.getuid()}', '--build-arg', f'GID={os.getgid()}',
       str(ROOT / 'container'), stdout=subprocess.DEVNULL)
    version = sh('docker', 'run', '--rm', IMAGE, *adapter.version, capture_output=True).stdout.strip()
    result = {'run_id': run_id, 'problem': problem.name, 'agent': agent_name, 'agent_version': version, 'model': model,
              'effort': effort, 'lang': lang, 'provider': upstream['host'] if upstream else 'direct',
              'upstream_commit': problem.commit,
              'started_at': started.isoformat()}
    initial = prepare_workspace(ws, problem) if problem.upstream else None
    harness_key = work / 'harness_key'
    keygen(harness_key, 'isu-bench-harness')
    agent_public_key = keygen(home / '.ssh' / 'id_ed25519', 'isu-bench-agent')
    if auth_store and auth_store.exists():
        auth = home / adapter.auth_file
        auth.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(auth_store, auth)
        auth.chmod(0o600)
    for path, text in (adapter.home_files(model, effort) if hasattr(adapter, 'home_files') else {}).items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)
    if hasattr(adapter, 'patch_text'):
        (home / Path(adapter.patch).name).write_text(adapter.patch_text(model, effort))
    registries = (ROOT / 'isubench' / 'registries.txt').read_text()

    infra = aws.Infra(work / 'aws.json', run_id)
    names = {'agent': f'{run_id}-agent', 'proxy': f'{run_id}-proxy', 'net': f'{run_id}-net'}
    server = None
    try:
        print('provisioning', flush=True)
        infra.provision(harness_key.with_suffix('.pub'), TTL_HOURS, problem)
        result['image'] = infra.state['image']
        if infra.state.get('images'):
            result['images'] = infra.state['images']
        h = hosts.Hosts(work, infra.state['hosts'], problem)
        h.write_configs(harness_key, home / '.ssh')
        print('preparing hosts', flush=True)
        result['host_resources'] = h.prepare(agent_public_key, lang, (ROOT / 'isubench' / 'proxy.py').read_text(), registries)
        audit(problem, infra, result, 'provisioned')
        if initial is None:
            # The initial implementation and docs ship in the AMI, so the workspace starts from s1 as booted.
            problem.fetch_workspace(h, ws)
            initial = commit_workspace(ws)

        (work / 'proxy' / 'allow.txt').write_text(registries + '\n'.join(
            adapter.domains + [f"{h.hosts[n]['public']}:22" for n in problem.apps]) + '\n')
        sh('docker', 'network', 'create', '--internal', names['net'], stdout=subprocess.DEVNULL)
        forward = []
        if upstream:
            key = work / 'proxy' / 'upstream-key'
            key.write_text(os.environ[upstream['credential']])
            key.chmod(0o600)
            forward = ['--forward', f"0.0.0.0:3129={upstream['host']}", '--bearer-file', '/data/upstream-key']
        sh('docker', 'run', '-d', '--name', names['proxy'], '-v', f"{ROOT / 'isubench' / 'proxy.py'}:/opt/proxy.py:ro",
           '-v', f"{work / 'proxy'}:/data", IMAGE, 'python3', '/opt/proxy.py', '--allow', '/data/allow.txt',
           '--log', '/data/proxy.jsonl', *forward, *ssh_controls(problem), stdout=subprocess.DEVNULL)
        sh('docker', 'network', 'connect', '--alias', 'proxy', names['net'], names['proxy'])
        env = {'HTTPS_PROXY': 'http://proxy:3128', 'HTTP_PROXY': 'http://proxy:3128', 'NO_PROXY': 'localhost,127.0.0.1,proxy',
               'NODE_USE_ENV_PROXY': '1', **adapter.env, **(upstream['env'] if upstream else {})}
        sh('docker', 'run', '-d', '--init', '--name', names['agent'], '--network', names['net'],
           '-v', f'{home}:/home/agent', '-v', f'{ws}:/work', '-v', f'{sock}:/run/isu-bench',
           *[a for k, v in env.items() for a in ('-e', f'{k}={v}')], *[a for c in credentials for a in ('-e', c)],
           IMAGE, 'sleep', 'infinity', stdout=subprocess.DEVNULL)

        lockdown = {'closed': False}

        def before(n, attempt):
            note = ''
            if problem.ssh_lockdown and n == benchd.LIMIT and not lockdown['closed']:
                reason = close_ssh(problem, infra, h, work, names['agent'], result)
                if reason:
                    return {'infra': f'closing SSH failed: {reason}'}
                lockdown['closed'] = True
                note = 'isu-bench: SSH to the servers is closed from this run until the session ends.\n'
            checks = problem.before_run(h, n, attempt) or {}
            return {**checks, 'message': note + checks.get('message', '')}

        server = benchd.BenchServer(sock / 'bench.sock', runner_for(problem, h, infra, res / 'bench', before=before))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        print('agent started', flush=True)
        agent_started = time.monotonic()
        prompt = render_prompt(problem, lang)
        logs = []
        for attempt in range(RESUMES + 1):
            if attempt:
                prompt = f'Continue. {benchd.LIMIT - len(server.runs)} of {benchd.LIMIT} benchmark runs remain.'
            log = res / 'agent' / f'session-{attempt}.jsonl'
            with log.open('w') as out, log.with_suffix('.stderr').open('w') as err:
                proc = subprocess.Popen(['docker', 'exec', '-w', '/work', names['agent'],
                                         *adapter.command(model, effort, prompt, logs)],
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=err)
                while proc.poll() is None and not server.finished.wait(5):
                    pass
                while not server.finished.is_set() and not server.idle():
                    time.sleep(5)
                if server.finished.is_set():
                    subprocess.run(['docker', 'kill', names['agent']], capture_output=True)
                proc.wait()
            logs.append(log)
            print(f'session {attempt} ended with {len(server.runs)} runs', flush=True)
            if server.finished.is_set():
                break
        result['resumes'] = len(logs) - 1
        result['agent_seconds'] = round(time.monotonic() - agent_started)
        result['status'] = ('aborted' if server.runner.aborted else
                            'completed' if server.finished.is_set() else 'stopped_early')

        if problem.ssh_lockdown:
            subprocess.run(['docker', 'kill', names['agent']], capture_output=True)
            if not lockdown['closed']:
                result['ssh_closed_after_stop'] = close_ssh(problem, infra, h, work, names['agent'], result) or 'ok'
            for name in (names['agent'], names['proxy']):
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        print('reboot check', flush=True)
        result['reboot_check'] = h.reboot_check(infra, server.runs[-1] if server.runs else None, res / 'bench')
        if result['reboot_check'].get('infra'):
            result['status'] = 'aborted'
        audit(problem, infra, result, 'after reboot')
        (res / 'proxy-hosts.jsonl').write_text(h.proxy_log())
    except BaseException as e:
        result['status'] = 'error'
        result['error'] = repr(e)
        raise
    finally:
        for name in (names['agent'], names['proxy']):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', names['net']], capture_output=True)
        result['runs'] = server.runs if server else []
        result['infra_failures'] = server.infra_failures if server else []
        last = result['runs'][-1] if result['runs'] else None
        result['final_score'] = last['score'] if last and last['passed'] and result.get('reboot_check', {}).get('passed') else 0
        result['finished_at'] = now().isoformat()
        if initial:
            sh('git', '-C', str(ws), 'add', '-A')
            (res / 'workspace.diff').write_text(
                sh('git', '-C', str(ws), 'diff', '--cached', '--binary', initial, capture_output=True).stdout)
        if (work / 'proxy' / 'proxy.jsonl').exists():
            shutil.copy(work / 'proxy' / 'proxy.jsonl', res / 'proxy-local.jsonl')
        if (home / '.dsh' / 'sessions').exists():
            subprocess.run(['docker', 'run', '--rm', '-v', f'{home}:/home/agent', IMAGE, 'node', '-e', UNZSTD],
                           capture_output=True)
        for d in SESSION_DIRS:
            if (home / d).exists():
                shutil.copytree(home / d, res / 'agent' / d, dirs_exist_ok=True)
        result['usage'] = adapter.usage(home)
        result['pace'] = pace(adapter, home, result['runs'])
        (res / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
        if auth_store and (home / adapter.auth_file).exists():
            shutil.copy(home / adapter.auth_file, auth_store)
            secrets += adapter.auth_secrets((home / adapter.auth_file).read_text())
        # The agent can see its own public IP in the servers' SSH login records.
        scrub(res, secrets + [infra.state['client_ip']] if infra.state.get('client_ip') else secrets)
        if keep:
            print(f'kept AWS resources; tear down with: python3 -m isubench teardown {run_id}', flush=True)
        else:
            print('tearing down', flush=True)
            infra.teardown()
    print(json.dumps({k: result[k] for k in ('run_id', 'status', 'final_score')}), flush=True)


def ssh_controls(problem):
    args = []
    if problem.ssh_throttle:
        delay, rate = problem.ssh_throttle
        args += ['--ssh-delay', str(delay), '--ssh-rate', str(rate)]
    if problem.ssh_lockdown:
        args += ['--ssh-closed', '/data/ssh-closed']
    return args


def calibrate(problem_name, count, deploy=None, keep=False):
    problem = PROBLEMS[problem_name]
    if deploy and not hasattr(problem, 'deploy'):
        raise SystemExit(f'{problem.name} has no deploy hook')
    started = now()
    run_id = f'calibrate-{problem.name}-{started:%Y%m%d%H%M%S}'
    work, res = ROOT / 'runs' / run_id, ROOT / 'results' / 'calibrate' / run_id
    (work / 'home' / '.ssh').mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    print(f'calibrate {run_id}', flush=True)
    harness_key = work / 'harness_key'
    keygen(harness_key, 'isu-bench-harness')
    agent_public_key = keygen(work / 'home' / '.ssh' / 'id_ed25519', 'isu-bench-agent')
    summary = {'run_id': run_id, 'problem': problem.name, 'deploy': Path(deploy).name if deploy else None,
               'count': count, 'started_at': started.isoformat()}
    infra = aws.Infra(work / 'aws.json', run_id)
    runner = None
    try:
        print('provisioning', flush=True)
        infra.provision(harness_key.with_suffix('.pub'), TTL_HOURS, problem)
        summary['images'] = infra.state.get('images') or infra.state['image']
        h = hosts.Hosts(work, infra.state['hosts'], problem)
        h.write_configs(harness_key, work / 'home' / '.ssh')
        print('preparing hosts', flush=True)
        summary['host_resources'] = h.prepare(agent_public_key, 'go', (ROOT / 'isubench' / 'proxy.py').read_text(),
                                              (ROOT / 'isubench' / 'registries.txt').read_text())
        audit(problem, infra, summary, 'provisioned')
        if deploy:
            print(f'deploying {deploy}', flush=True)
            problem.deploy(h, deploy)
        runner = runner_for(problem, h, infra, res / 'bench', limit=count)
        while not runner.finished.is_set():
            runner.execute(lambda text: print(text, end='', flush=True))
        print('reboot check', flush=True)
        summary['reboot_check'] = h.reboot_check(infra, runner.runs[-1] if runner.runs else None, res / 'bench')
        audit(problem, infra, summary, 'after reboot')
    except BaseException as e:
        summary['error'] = repr(e)
        raise
    finally:
        if runner:
            summary['runs'], summary['infra_failures'], summary['aborted'] = runner.runs, runner.infra_failures, runner.aborted
            scores = [r['score'] for r in runner.runs if r['passed']]
            if scores:
                mean = sum(scores) / len(scores)
                sd = (sum((x - mean) ** 2 for x in scores) / len(scores)) ** 0.5
                summary['scores'] = {'passed': len(scores), 'median': sorted(scores)[len(scores) // 2], 'mean': round(mean),
                                     'cv': round(sd / mean, 4) if mean else None}
        summary['finished_at'] = now().isoformat()
        (res / 'calibrate.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
        scrub(res, [infra.state['client_ip']] if infra.state.get('client_ip') else [])
        if keep:
            print(f'kept AWS resources; tear down with: python3 -m isubench teardown {run_id}', flush=True)
        else:
            print('tearing down', flush=True)
            infra.teardown()
    print(json.dumps({k: summary.get(k) for k in ('run_id', 'scores', 'reboot_check')}, ensure_ascii=False, default=str),
          flush=True)


def quota(problem_name, trials):
    problem = PROBLEMS[problem_name]
    vcpus = lambda types: {t['InstanceType']: t['VCpuInfo']['DefaultVCpus'] for t in aws.aws(
        'ec2', 'describe-instance-types', '--instance-types', *set(types))['InstanceTypes']}
    per_trial = sum(vcpus(problem.types.values())[t] for t in problem.types.values())
    limit = aws.aws('service-quotas', 'get-service-quota', '--service-code', 'ec2', '--quota-code', 'L-1216C47A')['Quota']['Value']
    running = [i for r in aws.aws('ec2', 'describe-instances', '--filters',
                                  'Name=instance-state-name,Values=pending,running,stopping')['Reservations']
               for i in r['Instances']]
    # The Standard quota covers the A, C, D, H, I, M, R, T and Z families (Inf is its own).
    standard = [i for i in running if i['InstanceType'][0] in 'acdhimrtz' and not i['InstanceType'].startswith('inf')]
    used = sum(i['CpuOptions']['CoreCount'] * i['CpuOptions']['ThreadsPerCore'] for i in standard)
    print(f'{problem.name}: {per_trial} vCPU per trial; {trials} trials need {per_trial * trials}')
    print(f'On-Demand Standard quota {limit:.0f} vCPU, in use {used}, free {limit - used:.0f}: '
          f'{int((limit - used) // per_trial)} trials fit')


def teardown(run_id):
    aws.Infra(ROOT / 'runs' / run_id / 'aws.json', run_id).teardown()


# Seconds of `pauses` (spans where a harness failure held the agent up, recorded by hand in result.json) before `until`.
def paused(pauses, until=math.inf):
    return sum(max(0, min(agents.epoch(p['to']), until) - agents.epoch(p['from'])) for p in pauses)


def agent_seconds(result):
    return result.get('agent_seconds', 0) - round(paused(result.get('pauses', [])))


# For each counted run: minutes from the agent's first action to the run's start, leaving out pauses, and the tool
# calls and output tokens since the previous counted run started.
def pace(adapter, home, runs, pauses=()):
    events = sorted(adapter.activity(home))
    if not events:
        return []
    out, i = [], 0
    for r in runs:
        start = agents.epoch(r['started_at'])
        calls = tokens = 0
        while i < len(events) and events[i][0] <= start:
            calls += events[i][1]
            tokens += events[i][2]
            i += 1
        out.append({'minutes': round((start - events[0][0] - paused(pauses, start)) / 60, 1), 'tool_calls': calls,
                    'output_tokens': tokens})
    return out


def backfill_pace():
    for f in sorted((ROOT / 'results').glob('*/result.json')):
        r = json.loads(f.read_text())
        if (f.parent / 'agent').exists() and r.get('runs') and r.get('agent') in agents.AGENTS:
            r['pace'] = pace(agents.AGENTS[r['agent']], f.parent / 'agent', r['runs'], r.get('pauses', []))
            f.write_text(json.dumps(r, indent=2, ensure_ascii=False) + '\n')
            print(f.parent.name, len(r['pace']))


def winner_ratio(problem, score):
    # A FAIL counts as no progress over the initial implementation, which keeps the geometric mean above zero.
    return max(score, problem.initial) / problem.winner


def table():
    results = [r for r in (json.loads(f.read_text()) for f in sorted((ROOT / 'results').glob('*/result.json')))
               if r.get('status') != 'aborted' and PROBLEMS[r['problem']].winner]
    configs = {}
    for r in results:
        configs.setdefault((r['agent'], r['model'], r['effort']), {})[r['problem']] = r
    names = [p for p in PROBLEMS if any(p in c for c in configs.values())]
    out = ['| agent | model | effort | score | ' + ' | '.join(names) + ' |',
           '|---|---|---|---:|' + '---:|' * len(names)]
    board = []
    for (agent, model, effort), by_problem in configs.items():
        scored = [p for p in names if PROBLEMS[p].scored]
        ratios = [winner_ratio(PROBLEMS[p], by_problem[p]['final_score']) for p in scored if p in by_problem]
        score = math.prod(ratios) ** (1 / len(ratios)) if ratios and len(ratios) == len(scored) else None
        cells = [f"{by_problem[p]['final_score']:,} ({winner_ratio(PROBLEMS[p], by_problem[p]['final_score']):.0%})"
                 if p in by_problem else '-' for p in names]
        board.append((score or 0, f"| {agent} | {model} | {effort} | {f'{score:.0%}' if score else '-'} | "
                                   + ' | '.join(cells) + ' |'))
    out += [row for _, row in sorted(board, reverse=True)]
    out.append('')
    for name in names:
        problem = PROBLEMS[name]
        out += [f'### {problem.title}', '',
                '| agent | model | effort | final | of winner | x initial | runs | agent time | output | cache read | API $ |',
                '|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|']
        for r in sorted((r for r in results if r['problem'] == name), key=lambda r: -r['final_score']):
            u = r.get('usage', {})
            runs = ' '.join(str(x['score'] if x['passed'] else 'FAIL') for x in r['runs'])
            out.append(f"| {r['agent']} {r['agent_version']} | {r['model']} | {r['effort']} | {r['final_score']:,} | "
                       f"{r['final_score'] / problem.winner:.0%} | {r['final_score'] / problem.initial:.0f} | {runs} | "
                       f"{agent_seconds(r) // 60} min | {u.get('output_tokens', 0):,} | {u.get('cache_read_tokens', 0):,} | "
                       f"{prices.cost(r['model'], u) or 0:,.2f} |")
        out.append('')
    print('\n'.join(out))
