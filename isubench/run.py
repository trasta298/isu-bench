import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import agents, aws, benchd, hosts

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = 'https://github.com/isucon/isucon14.git'
UPSTREAM_COMMIT = '53f8b627e040c30ebec600457c6c97da008b84b0'
IMAGE = 'isu-bench-agent:v1'
LANGS = {'go': 'Go', 'rust': 'Rust', 'node': 'Node.js', 'perl': 'Perl', 'php': 'PHP', 'python': 'Python', 'ruby': 'Ruby'}
RESUMES = 3
TTL_HOURS = 48
SESSION_DIRS = ('.claude/projects', '.codex/sessions', '.dsh/sessions')


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def prepare_workspace(dest: Path):
    cache = ROOT / 'runs' / '.upstream'
    if not cache.exists():
        sh('git', 'clone', '-q', '--filter=blob:none', '--no-checkout', UPSTREAM, str(cache))
    sh('git', '-C', str(cache), 'checkout', '-q', UPSTREAM_COMMIT)
    for d in ('docs', 'webapp'):
        shutil.copytree(cache / d, dest / d)
    sh('git', 'init', '-q', str(dest))
    sh('git', '-C', str(dest), 'add', '-A')
    sh('git', '-C', str(dest), '-c', 'user.name=isu-bench', '-c', 'user.email=isu-bench@localhost', 'commit', '-qm', 'initial')
    return sh('git', '-C', str(dest), 'rev-parse', 'HEAD', capture_output=True).stdout.strip()


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


def run(agent_name, model, effort, lang, keep=False):
    adapter = agents.AGENTS[agent_name]
    credentials = [c for c in adapter.credentials if os.environ.get(c)]
    auth_store = getattr(adapter, 'auth_store', None)
    if not credentials and not (auth_store and auth_store.exists()):
        raise SystemExit(f'set one of {", ".join(adapter.credentials)}' + (f' or create {auth_store}' if auth_store else ''))
    secrets = [os.environ[c] for c in credentials]
    started = now()
    run_id = re.sub(r'[^a-z0-9-]+', '-', f'{agent_name}-{model}-{lang}-{started:%Y%m%d%H%M%S}'.lower())
    work, res = ROOT / 'runs' / run_id, ROOT / 'results' / run_id
    home, ws, sock = work / 'home', work / 'workspace', work / 'sock'
    for d in (home / '.ssh', ws, sock, work / 'proxy', res / 'bench', res / 'agent'):
        d.mkdir(parents=True, exist_ok=True)
    print(f'run {run_id}', flush=True)

    sh('docker', 'build', '-q', '-t', IMAGE, '--build-arg', f'UID={os.getuid()}', '--build-arg', f'GID={os.getgid()}',
       str(ROOT / 'container'), stdout=subprocess.DEVNULL)
    version = sh('docker', 'run', '--rm', IMAGE, *adapter.version, capture_output=True).stdout.strip()
    result = {'run_id': run_id, 'agent': agent_name, 'agent_version': version, 'model': model, 'effort': effort,
              'lang': lang, 'upstream_commit': UPSTREAM_COMMIT, 'started_at': started.isoformat()}
    initial = prepare_workspace(ws)
    harness_key = work / 'harness_key'
    keygen(harness_key, 'isu-bench-harness')
    agent_public_key = keygen(home / '.ssh' / 'id_ed25519', 'isu-bench-agent')
    if auth_store and auth_store.exists():
        auth = home / adapter.auth_file
        auth.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(auth_store, auth)
        auth.chmod(0o600)
    if hasattr(adapter, 'patch_text'):
        (home / Path(adapter.patch).name).write_text(adapter.patch_text(model, effort))
    registries = (ROOT / 'isubench' / 'registries.txt').read_text()

    infra = aws.Infra(work / 'aws.json', run_id)
    names = {'agent': f'{run_id}-agent', 'proxy': f'{run_id}-proxy', 'net': f'{run_id}-net'}
    server = None
    try:
        print('provisioning', flush=True)
        infra.provision(harness_key.with_suffix('.pub'), TTL_HOURS)
        result['image'] = infra.state['image']
        h = hosts.Hosts(work, infra.state['hosts'])
        h.write_configs(harness_key, home / '.ssh')
        print('preparing hosts', flush=True)
        result['host_resources'] = h.prepare(agent_public_key, lang, (ROOT / 'isubench' / 'proxy.py').read_text(), registries)

        (work / 'proxy' / 'allow.txt').write_text(registries + '\n'.join(
            adapter.domains + [f"{h.hosts[n]['public']}:22" for n in hosts.APPS]) + '\n')
        sh('docker', 'network', 'create', '--internal', names['net'], stdout=subprocess.DEVNULL)
        sh('docker', 'run', '-d', '--name', names['proxy'], '-v', f"{ROOT / 'isubench' / 'proxy.py'}:/opt/proxy.py:ro",
           '-v', f"{work / 'proxy'}:/data", IMAGE, 'python3', '/opt/proxy.py', '--allow', '/data/allow.txt',
           '--log', '/data/proxy.jsonl', stdout=subprocess.DEVNULL)
        sh('docker', 'network', 'connect', '--alias', 'proxy', names['net'], names['proxy'])
        env = {'HTTPS_PROXY': 'http://proxy:3128', 'HTTP_PROXY': 'http://proxy:3128', 'NO_PROXY': 'localhost,127.0.0.1',
               'NODE_USE_ENV_PROXY': '1', **adapter.env}
        sh('docker', 'run', '-d', '--init', '--name', names['agent'], '--network', names['net'],
           '-v', f'{home}:/home/agent', '-v', f'{ws}:/work', '-v', f'{sock}:/run/isu-bench',
           *[a for k, v in env.items() for a in ('-e', f'{k}={v}')], *[a for c in credentials for a in ('-e', c)],
           IMAGE, 'sleep', 'infinity', stdout=subprocess.DEVNULL)

        server = benchd.BenchServer(sock / 'bench.sock', res / 'bench',
                                    ['ssh', '-F', str(h.config), '-o', 'BatchMode=yes', 'bench'], h.bench_command())
        threading.Thread(target=server.serve_forever, daemon=True).start()

        print('agent started', flush=True)
        agent_started = time.monotonic()
        prompt = (ROOT / 'prompt.md').read_text().replace('{LANG}', LANGS[lang]).replace('{lang}', lang).replace('{RUNS}', str(benchd.LIMIT))
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
        result['status'] = 'completed' if server.finished.is_set() else 'stopped_early'
        result['usage'] = adapter.usage(logs, home)

        print('reboot check', flush=True)
        result['reboot_check'] = h.reboot_check(infra)
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
        last = result['runs'][-1] if result['runs'] else None
        result['final_score'] = last['score'] if last and last['passed'] and result.get('reboot_check', {}).get('passed') else 0
        result['finished_at'] = now().isoformat()
        sh('git', '-C', str(ws), 'add', '-A')
        (res / 'workspace.diff').write_text(
            sh('git', '-C', str(ws), 'diff', '--cached', '--binary', initial, capture_output=True).stdout)
        if (work / 'proxy' / 'proxy.jsonl').exists():
            shutil.copy(work / 'proxy' / 'proxy.jsonl', res / 'proxy-local.jsonl')
        for d in SESSION_DIRS:
            if (home / d).exists():
                shutil.copytree(home / d, res / 'agent' / d, dirs_exist_ok=True)
        (res / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
        if auth_store and (home / adapter.auth_file).exists():
            shutil.copy(home / adapter.auth_file, auth_store)
            secrets += adapter.auth_secrets((home / adapter.auth_file).read_text())
        scrub(res, secrets)
        if keep:
            print(f'kept AWS resources; tear down with: python3 -m isubench teardown {run_id}', flush=True)
        else:
            print('tearing down', flush=True)
            infra.teardown()
    print(json.dumps({k: result[k] for k in ('run_id', 'status', 'final_score')}), flush=True)


def teardown(run_id):
    aws.Infra(ROOT / 'runs' / run_id / 'aws.json', run_id).teardown()


def table():
    rows = ['| agent | model | lang | runs | final | status | agent time | input | output | cache read | cost |',
            '|---|---|---|---|---|---|---|---|---|---|---|']
    for f in sorted((ROOT / 'results').glob('*/result.json')):
        r = json.loads(f.read_text())
        u = r.get('usage', {})
        scores = ' '.join(str(x['score'] if x['passed'] else 'FAIL') for x in r['runs'])
        cost = f"${u['cost_usd']:.2f}" if u.get('cost_usd') else '-'
        rows.append(f"| {r['agent']} {r['agent_version']} | {r['model']} | {r['lang']} | {scores} | {r['final_score']} | "
                    f"{r['status']} | {r.get('agent_seconds', 0) // 60} min | {u.get('input_tokens', '-')} | "
                    f"{u.get('output_tokens', '-')} | {u.get('cache_read_tokens', '-')} | {cost} |")
    print('\n'.join(rows))
