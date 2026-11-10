import base64
import json
import re
import subprocess
import time
from pathlib import Path

from .hosts import PROXY_PORT

LANGS = {'go': 'Go', 'rust': 'Rust', 'node': 'Node.js', 'perl': 'Perl', 'php': 'PHP', 'python': 'Python', 'ruby': 'Ruby'}
CONTEST_TYPES = {'s1': 'c5.large', 's2': 'c5.large', 's3': 'c5.large', 'bench': 'c5.2xlarge'}


# Defaults reproduce the v1 setup, so a problem overrides only what differs.
class Problem:
    apps = ('s1', 's2', 's3')
    # Only these count toward the isu-bench score; public problems may have their solutions in training data.
    scored = False
    # Results of an earlier version of the problem: shown apart from the current ones.
    retired = False
    langs = tuple(LANGS)
    upstream = None
    # role -> fixed private IP and extra private IPs on the same interface; empty means AWS picks.
    addresses = {}
    secondary_addresses = {}
    # The harness reaches the apps through this host instead of directly (used to close the agent's SSH path).
    harness_jump = None
    # Close the agent's SSH from the start of the last run until the post-reboot check ends.
    ssh_lockdown = False
    # (one-way delay in seconds, bytes per second) the sidecar proxy puts on the agent's SSH.
    ssh_throttle = None
    # Compare security groups, interfaces and routes with the problem's definition through the AWS API.
    audit_network = False
    # Runs judged as benchmarker failures are not counted; past this many the session is aborted.
    infra_limit = None
    # (source, port) per group: a group name or 'client' (the caller's public IP); port None means all traffic.
    ingress = {'app': [('app', None), ('bench', None), ('client', 22), ('client', 443)],
               'bench': [('app', None), ('client', 22)]}
    # Groups listed here lose the default allow-all egress; the apps reach the internet only through the proxy.
    egress = {'app': ['app', 'bench']}

    @property
    def amis(self):
        return {role: self.ami for role in self.types}

    @property
    def groups(self):
        return {role: 'bench' if role == 'bench' else 'app' for role in self.types}

    @property
    def no_proxy_hosts(self):
        return (self.target_host,)

    def check_ready(self, h):
        if h.http_status('/') != 200:
            raise RuntimeError('s1 does not serve https after setup')

    def run_command(self, hosts, attempt, runs):
        return self.bench_command(hosts)

    def outcome(self, stdout, stderr):
        passed, score = self.parse(stdout, stderr)
        return {'passed': passed, 'score': score, 'infra': False}

    def prepare_apps(self, h, work):
        pass

    def before_run(self, h, n, attempt):
        return None

    def after_run(self, h, n, attempt, result):
        return None

    def recover(self, h, infra):
        pass


class Isucon14(Problem):
    name = 'isucon14'
    title = 'ISUCON14'
    ami = 'ami-0fcf9e8e8675a9ee4'
    types = CONTEST_TYPES
    upstream = 'https://github.com/isucon/isucon14.git'
    commit = '53f8b627e040c30ebec600457c6c97da008b84b0'
    service = 'isuride'
    # https://isucon.net/archives/58837992.html
    winner = 58_153
    # Median first-run score of the initial Go implementation in this environment.
    initial = 961
    target_host = 'isuride.xiv.isucon.net'
    probe_paths = ('/', '/api/app/rides')
    bench_note = 'It sends load to s1 for 60 seconds and prints the result.'
    result = re.compile(r'msg=結果 pass=(true|false) スコア=(-?\d+)')

    # The AMI runs its own payment mock on 12345, where the benchmarker must listen instead.
    bench_ready = "! ss -ltn | grep -q ':12345 '"

    def prepare_bench(self, h, work):
        h.ssh('bench', "sudo systemctl disable --now nginx mysql $(systemctl list-unit-files --type=service "
                       "--no-legend --plain 'isuride*' | awk '{print $1}')")

    def bench_command(self, hosts):
        return (f"sudo -iu isucon ./bench run --target https://{self.target_host} --addr {hosts['s1']['private']}:443 "
                f"--payment-url http://{hosts['bench']['private']}:12345 -t 60")

    def parse(self, stdout, stderr):
        match = self.result.findall(stdout)
        return (bool(match) and match[-1][0] == 'true', int(match[-1][1]) if match else None)


class Isucon13(Problem):
    name = 'isucon13'
    title = 'ISUCON13'
    ami = 'ami-041289d910c114864'
    types = CONTEST_TYPES
    upstream = 'https://github.com/isucon/isucon13.git'
    # The commit the official AMI was built from (its name starts with this hash).
    commit = '5895cf1b1c77bbbd4d81317b6307564184caafb4'
    service = 'isupipe'
    # https://isucon.net/archives/57993937.html
    winner = 468_006
    initial = 3_673
    target_host = 'pipe.u.isucon.dev'
    probe_paths = ('/', '/api/tag')
    bench_note = ('It sends load to https://pipe.u.isucon.dev for 60 seconds, resolving names through the DNS '
                  'server on s1, and prints the result.')
    # Only a passing run logs its score; a failed pretest or final check ends without one.
    result = re.compile(r'スコア: (-?\d+)')
    bench_ready = 'true'

    def certificates(self, work):
        d = work / 'tls'
        if not (d / 'server.crt').exists():
            d.mkdir(exist_ok=True)
            run = lambda *a: subprocess.run(a, check=True, capture_output=True, cwd=d)
            run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30', '-subj', '/CN=isu-bench CA',
                '-keyout', 'ca.key', '-out', 'ca.crt')
            run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=*.u.isucon.dev',
                '-keyout', 'server.key', '-out', 'server.csr')
            (d / 'server.ext').write_text('subjectAltName=DNS:*.u.isucon.dev,DNS:u.isucon.dev\n')
            run('openssl', 'x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial',
                '-days', '30', '-extfile', 'server.ext', '-out', 'server.crt')
        return d

    # The certificate shipped in the AMI has expired, so the harness issues its own and the benchmark host trusts it.
    def prepare_bench(self, h, work):
        d = self.certificates(work)
        h.ssh('bench', "sudo systemctl disable --now nginx mysql pdns $(systemctl list-unit-files --type=service "
                       "--no-legend --plain 'isupipe*' | awk '{print $1}')")
        h.ssh('bench', 'sudo tee /usr/local/share/ca-certificates/isu-bench.crt >/dev/null && '
                       'sudo update-ca-certificates >/dev/null', input=(d / 'ca.crt').read_text())

    def prepare_apps(self, h, work):
        d = self.certificates(work)
        for host in ('s1', 's2', 's3'):
            ip = h.hosts[host]['private']
            h.ssh(host, 'sudo tee /etc/nginx/tls/_.u.isucon.dev.crt >/dev/null', input=(d / 'server.crt').read_text())
            h.ssh(host, 'sudo install -m 600 /dev/stdin /etc/nginx/tls/_.u.isucon.dev.key', input=(d / 'server.key').read_text())
            h.ssh(host, f"sudo sed -i 's/^ISUCON13_POWERDNS_SUBDOMAIN_ADDRESS=.*/ISUCON13_POWERDNS_SUBDOMAIN_ADDRESS=\"{ip}\"/' "
                        f"/home/isucon/env.sh && sudo mysql -e \"UPDATE isudns.records SET content='{ip}' WHERE type='A'\" && "
                        "sudo systemctl restart isupipe-go && sudo nginx -t -q && sudo systemctl reload nginx")

    def bench_command(self, hosts):
        return (f"sudo -iu isucon ./bench_linux_amd64 run --target https://{self.target_host} "
                f"--nameserver {hosts['s1']['private']} --enable-ssl "
                f"--webapp {hosts['s2']['private']} --webapp {hosts['s3']['private']}")

    def parse(self, stdout, stderr):
        match = self.result.findall(stderr)
        return (bool(match), int(match[-1]) if match else None)


# The router address each server is reached at from every other node, which is also the source the others see.
ISUMAIL_VIA = {'s1': '10.0.1.21', 's2': '10.0.1.22', 's3': '10.0.1.23', 'bench': '10.0.1.20'}
ISUMAIL_NAMES = {'s1': 's1.internal', 's2': 's2.internal', 's3': 's3.internal', 'bench': 'provider.internal'}
# The router's uplink per server in MiB/s, shared by everything the server sends.
ISUMAIL_UPLINK_MIB = {'s1': 12, 's2': 18, 's3': 6}
ISUMAIL_IPERF_PORT = 5201
ISUMAIL_BENCH = '/opt/isumail/bin/bench'
ISUMAIL_STATE_DIR = '/var/lib/isumail-bench'
TIME_SOURCE = '169.254.169.123'
CLOCK_TOLERANCE_MS = 50
JSON_LINE = re.compile(r'^\{.*\}$', re.M)


def last_json(text):
    lines = JSON_LINE.findall(text)
    return json.loads(lines[-1]) if lines else None


class Isumail(Problem):
    name = 'isumail'
    title = 'ISUMAIL'
    # Built by isu-new/ami (not in this repository); the source reaches the agent only through s1 of a run.
    amis = {'s1': 'ami-0c07931bc35138ebf', 's2': 'ami-0386de926573364e3', 's3': 'ami-0386de926573364e3',
            'router': 'ami-0cc8249ffb25653a1', 'bench': 'ami-06ede16f4cb5d89d5'}
    types = {'s1': 'c5.large', 's2': 'c7g.medium', 's3': 'c6g.medium', 'router': 'c6g.medium', 'bench': 'c7g.2xlarge'}
    # Fixed so the AMIs can carry /etc/hosts and the router's rules; the router's primary address stands for the bench.
    addresses = {'s1': '10.0.1.11', 's2': '10.0.1.12', 's3': '10.0.1.13', 'router': '10.0.1.20', 'bench': '10.0.1.30'}
    secondary_addresses = {'router': (ISUMAIL_VIA['s1'], ISUMAIL_VIA['s2'], ISUMAIL_VIA['s3'])}
    groups = {'s1': 'app', 's2': 'app', 's3': 'app', 'router': 'router', 'bench': 'bench'}
    # The router is the only path between the servers and to the provider; the bench keeps its SSH jump to the apps
    # and the apps their internet proxy on the bench.
    ingress = {'app': [('router', None), ('bench', 22), ('client', 22)],
               'router': [('app', None), ('bench', None), ('client', 22)],
               'bench': [('router', None), ('app', PROXY_PORT), ('client', 22)]}
    egress = {'app': ['router', 'bench']}
    langs = ('go',)
    harness_jump = 'bench'
    ssh_lockdown = True
    ssh_throttle = (0.1, 20_000_000 / 8)
    audit_network = True
    infra_limit = 5
    scored = True
    commit = None
    service = 'isumail'
    # winner is the estimated theoretical maximum; initial is the median of 5 `calibrate` runs of the reference
    # implementation on AWS (2026-11-11, v6).
    winner = 1_060_000
    initial = 1_744
    probe = ('/api/me', 401)
    no_proxy_hosts = ('.internal', '.isumail.test')
    bench_note = ('It sends isumail.test to s1 and t.isumail.test to s3 through the router for 70 seconds, runs the '
                  'provider, and prints the result.')
    bench_ready = f'test -x {ISUMAIL_BENCH}'

    def targets(self, hosts):
        return (f"--target app=http://{ISUMAIL_VIA['s1']} --target t=http://{ISUMAIL_VIA['s3']} "
                + ' '.join(f'--sender {h}={ISUMAIL_VIA[h]}' for h in self.apps))

    # Every attempt, counted or not, writes its own file, so a run that saved nothing cannot leave an older state
    # behind for persist-check.
    def state_path(self, attempt):
        return f'{ISUMAIL_STATE_DIR}/state-{attempt:02d}.json'

    def fetch_workspace(self, h, dest):
        out = h.ssh('s1', 'sudo tar -C /home/isucon -cz --exclude=webapp/go/isumail '
                          '--exclude=webapp/guard/isumail-guard webapp docs | base64 -w0').stdout
        subprocess.run(['tar', '-xz', '-C', str(dest)], input=base64.b64decode(out), check=True)

    def prepare_bench(self, h, work):
        h.ssh('bench', f'sudo install -d -o ubuntu -g ubuntu {ISUMAIL_STATE_DIR} && rm -f {ISUMAIL_STATE_DIR}/state-*.json')

    def prepare_router(self, h):
        h.ssh('router', 'sudo isumail-router check')

    def prepare_apps(self, h, work):
        self.check_network(h)

    # Runs before the agent gets the servers: afterwards their root could fake every answer.
    def check_network(self, h):
        # 124 is timeout's status: the security groups drop the packets, where a closed port would refuse at once.
        connect = lambda src, ip: h.ssh(src, f"timeout 3 bash -c '</dev/tcp/{ip}/22'", check=False).returncode
        for src in self.apps:
            for dst in (*self.apps, 'bench'):
                if dst == src:
                    continue
                if connect(src, ISUMAIL_NAMES[dst]) != 0:
                    raise RuntimeError(f'{src} does not reach {ISUMAIL_NAMES[dst]} through the router')
                if connect(src, h.hosts[dst]['private']) != 124:
                    raise RuntimeError(f'{src} is not blocked from {dst} at {h.hosts[dst]["private"]}')
        for dst in self.apps:
            if h.ssh('bench', f"timeout 3 bash -c '</dev/tcp/{h.hosts[dst]['private']}/80'", check=False).returncode != 124:
                raise RuntimeError(f'bench is not blocked from {dst} at {h.hosts[dst]["private"]}:80')
        h.ssh('bench', f'pkill -x iperf3; iperf3 -s -D -p {ISUMAIL_IPERF_PORT}', check=False)
        try:
            for src in self.apps:
                out = h.ssh(src, f'iperf3 -J -t 4 -c {ISUMAIL_NAMES["bench"]} -p {ISUMAIL_IPERF_PORT}').stdout
                mib = json.loads(out)['end']['sum_received']['bits_per_second'] / 8 / 2**20
                want = ISUMAIL_UPLINK_MIB[src]
                # Goodput is the limit less the TCP/IP headers (about 1% at MTU 9001) and the slow start.
                if not 0.85 * want <= mib <= 1.02 * want:
                    raise RuntimeError(f'{src} -> {ISUMAIL_NAMES["bench"]}: {mib:.2f} MiB/s, expected {want} MiB/s')
        finally:
            h.ssh('bench', 'pkill -x iperf3', check=False)

    def probe_command(self, hosts, host):
        path, _ = self.probe
        return (f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 -H 'Host: isumail.test' "
                f"http://{ISUMAIL_VIA[host]}{path}")

    def probe_statuses(self, h, limit=300):
        _, want = self.probe
        deadline = time.monotonic() + limit
        while True:
            statuses = {host: int(h.ssh('bench', self.probe_command(h.hosts, host), check=False).stdout or 0)
                        for host in ('s1',)}
            if all(v == want for v in statuses.values()) or time.monotonic() > deadline:
                return statuses
            time.sleep(10)

    def check_ready(self, h):
        statuses = self.probe_statuses(h)
        if any(v != self.probe[1] for v in statuses.values()):
            raise RuntimeError(f'the apps do not answer {self.probe}: {statuses}')

    def run_command(self, hosts, attempt, runs):
        previous = next((self.state_path(r['attempt']) for r in reversed(runs)
                         if r.get('passed') and r.get('state_sha256')), None)
        return (f"{ISUMAIL_BENCH} run {self.targets(hosts)} --provider-listen :8025 "
                f"--provider-url http://{ISUMAIL_NAMES['bench']}:8025 --state {self.state_path(attempt)}"
                + (f' --previous-state {previous}' if previous else ''))

    def outcome(self, stdout, stderr):
        last = last_json(stderr)
        if last is None:
            return {'passed': False, 'score': None, 'infra': True, 'reason': 'no result line'}
        result = {'passed': bool(last.get('pass')), 'score': last.get('score'), 'infra': bool(last.get('infra_error')),
                  'run_id': last.get('run_id') or None, 'state_sha256': last.get('state_sha256') or None}
        # A pass that cannot be tied to a saved state cannot be checked after the reboot, and the fault is the
        # benchmarker's, not the app's.
        if result['passed'] and not (result['run_id'] and result['state_sha256']):
            result.update(passed=False, infra=True, reason='pass without run_id/state_sha256')
        return result

    def persist_command(self, hosts, state, run_id, state_sha256):
        if not (state and run_id and state_sha256):
            raise ValueError('persist-check needs the state, run_id and state_sha256 of the last counted run')
        return (f'{ISUMAIL_BENCH} persist-check {self.targets(hosts)} --state {state} '
                f'--run-id {run_id} --state-sha256 {state_sha256}')

    def parse_persist(self, stdout, stderr):
        last = last_json(stderr)
        if last is None:
            return {'passed': False, 'infra': True}
        return {'passed': bool(last.get('pass')), 'infra': False, 'errors': last.get('errors')}

    def reboot_verdict(self, rebooted, statuses, persist):
        _, want = self.probe
        return {'passed': all(rebooted.get(h) for h in self.apps) and all(v == want for v in statuses.values())
                and persist.get('passed', False),
                'rebooted': rebooted, 'probe_status': statuses, 'persist': persist}

    def verify_reboot(self, h, rebooted, last, out):
        statuses = self.probe_statuses(h)
        if not (last and last.get('passed') and last.get('run_id') and last.get('state_sha256')):
            persist = {'passed': False, 'infra': False, 'skipped': 'the last counted run has no state'}
        else:
            command = self.persist_command(h.hosts, self.state_path(last['attempt']), last['run_id'], last['state_sha256'])
            for attempt in (1, 2):
                proc = h.ssh('bench', command, timeout=900, check=False)
                (out / f'persist-{attempt}.stdout').write_text(proc.stdout)
                (out / f'persist-{attempt}.stderr').write_text(proc.stderr)
                persist = self.parse_persist(proc.stdout, proc.stderr)
                if not persist['infra']:
                    break
        verdict = self.reboot_verdict(rebooted, statuses, persist)
        router = self.router_stats(h)
        verdict['router'] = router
        verdict['infra'] = persist['infra'] or not router['ok']
        return verdict

    def router_stats(self, h):
        proc = h.ssh('router', 'sudo isumail-router check; sudo isumail-router stats; head -1 /proc/stat', check=False)
        return {'ok': proc.returncode == 0 and 'isumail-router: ok' in proc.stdout, 'text': proc.stdout}

    def clock_sample(self, h, host):
        proc = h.ssh(host, 'chronyc -n -c tracking', timeout=30, check=False)
        fields = proc.stdout.strip().split(',')
        if proc.returncode or len(fields) < 14:
            return {'source': None, 'synced': False, 'offset_ms': None, 'error_ms': None}
        offset, delay, dispersion = (float(fields[i]) * 1000 for i in (4, 10, 11))
        return {'source': fields[1], 'synced': fields[13] == 'Normal', 'offset_ms': offset,
                # chrony's bound on the distance from true time: the offset plus half the root delay and the dispersion.
                'error_ms': abs(offset) + delay / 2 + dispersion}

    def clock_check(self, h):
        samples = {host: self.clock_sample(h, host) for host in (*self.apps, 'router', 'bench')}
        healthy = lambda s: s['synced'] and s['source'] == TIME_SOURCE
        bench = samples['bench']
        reasons = [] if healthy(bench) else [f'bench is not synced to {TIME_SOURCE}']
        for host in self.apps:
            s = samples[host]
            # The agent's root can change chrony on the apps; that is its own doing and must not buy a free run.
            if healthy(s) and healthy(bench) and s['error_ms'] + bench['error_ms'] > CLOCK_TOLERANCE_MS:
                reasons.append(f"{host} may differ from bench by {s['error_ms'] + bench['error_ms']:.1f} ms")
        return {'infra': bool(reasons), 'reasons': reasons, 'samples': samples}

    def checks(self, h):
        router, clock = self.router_stats(h), self.clock_check(h)
        reasons = ([] if router['ok'] else ['router check failed']) + clock['reasons']
        return {'infra': '; '.join(reasons) or None, 'files': {'router.txt': router['text'], 'clock.json': json.dumps(clock)}}

    def before_run(self, h, n, attempt):
        return self.checks(h)

    def after_run(self, h, n, attempt, result):
        return self.checks(h)

    def recover(self, h, infra):
        h.ssh('router', 'sudo systemctl restart isumail-router', check=False)
        h.ssh('bench', 'sudo systemctl restart chrony', check=False)
        time.sleep(30)

    def close_sessions(self, h, agent_user='isucon'):
        left = {}
        for host in self.apps:
            # Only sshd's session processes: the app runs as the same user under systemd and keeps running.
            peers = h.ssh(host, f"sudo pkill -KILL -f '^sshd: {agent_user}( |@)'; sleep 1; "
                                "ss -Htn state established '( sport = :22 )' | awk '{print $4}'", check=False).stdout.split()
            others = [p for p in peers if p.rsplit(':', 1)[0] != h.hosts['bench']['private']]
            if others:
                left[host] = others
        return left

    # The tarball holds the contents of webapp/ (go/, sql/, etc/, ...); a deploy.sh at its top replaces the default steps.
    def deploy(self, h, tarball):
        data = base64.b64encode(Path(tarball).read_bytes()).decode()
        for host in self.apps:
            h.ssh(host, 'sudo rm -rf /tmp/isumail-deploy && mkdir /tmp/isumail-deploy && '
                        'base64 -d | tar -xz -C /tmp/isumail-deploy', input=data)
            h.ssh(host, f'sudo bash -s {host}', timeout=1800, input=DEPLOY_SCRIPT)
        self.check_ready(h)

    def security_group_differences(self, described, sg, client_ip, ssh_closed=False):
        def normalize(permissions):
            out = set()
            for p in permissions:
                ports = (p.get('FromPort'), p.get('ToPort')) if p['IpProtocol'] != '-1' else (None, None)
                for kind, key, field in (('cidr', 'IpRanges', 'CidrIp'), ('cidr6', 'Ipv6Ranges', 'CidrIpv6'),
                                         ('group', 'UserIdGroupPairs', 'GroupId'), ('prefix', 'PrefixListIds', 'PrefixListId')):
                    out |= {(p['IpProtocol'], *ports, kind, r[field]) for r in p.get(key, [])}
            return out

        def expected(rules):
            out = set()
            for source, port in rules:
                kind, value = ('cidr', f'{client_ip}/32') if source == 'client' else ('group', sg[source])
                out.add(('-1', None, None, kind, value) if port is None else ('tcp', port, port, kind, value))
            return out

        by_id = {g['GroupId']: g for g in described}
        diffs = []
        for name, gid in sg.items():
            g = by_id.get(gid)
            if g is None:
                diffs.append(f'{name}: missing')
                continue
            want_egress = (expected([(s, None) for s in self.egress[name]]) if name in self.egress
                           else {('-1', None, None, 'cidr', '0.0.0.0/0')})
            ingress = [r for r in self.ingress[name] if not (ssh_closed and name == 'app' and r == ('client', 22))]
            for direction, want, got in (('ingress', expected(ingress), normalize(g['IpPermissions'])),
                                         ('egress', want_egress, normalize(g['IpPermissionsEgress']))):
                if want != got:
                    diffs.append(f'{name} {direction}: missing {sorted(want - got, key=str)}, '
                                 f'unexpected {sorted(got - want, key=str)}')
        return diffs

    def interface_differences(self, described, sg, instances):
        roles = {iid: role for role, iid in instances.items()}
        diffs = []
        for eni in described:
            role = roles.get((eni.get('Attachment') or {}).get('InstanceId'))
            if role is None:
                diffs.append(f"{eni['NetworkInterfaceId']}: not attached to a run instance")
                continue
            want = {self.addresses[role], *self.secondary_addresses.get(role, ())}
            got = {a['PrivateIpAddress'] for a in eni['PrivateIpAddresses']}
            if got != want:
                diffs.append(f'{role}: addresses {sorted(got)}, expected {sorted(want)}')
            if {g['GroupId'] for g in eni['Groups']} != {sg[self.groups[role]]}:
                diffs.append(f'{role}: security groups {[g["GroupId"] for g in eni["Groups"]]}')
            if eni.get('Ipv6Addresses'):
                diffs.append(f'{role}: has IPv6 addresses')
            if not eni.get('SourceDestCheck', True):
                diffs.append(f'{role}: source/dest check is off')
        if len(described) != len(instances):
            diffs.append(f'expected one interface per instance, found {len(described)}')
        return diffs


DEPLOY_SCRIPT = r"""set -euo pipefail
role=$1
rm -rf /home/isucon/webapp
mv /tmp/isumail-deploy /home/isucon/webapp
chown -R isucon:isucon /home/isucon/webapp
isumail-role env "$role"
cd /home/isucon/webapp
if [ -f deploy.sh ]; then
  exec bash deploy.sh "$role"
fi
isumail-role nginx "$role"
nginx -t -q && systemctl reload nginx
if ls etc/systemd/*.service >/dev/null 2>&1; then
  install -m 644 etc/systemd/*.service /etc/systemd/system/
  systemctl daemon-reload
fi
for pair in go:isumail guard:isumail-guard; do
  dir=${pair%%:*} bin=${pair#*:}
  if [ -f "$dir/go.mod" ]; then
    sudo -iu isucon bash -c "cd ~/webapp/$dir && go build -o $bin ."
    systemctl enable -q "isumail-$dir"
    systemctl restart "isumail-$dir"
  else
    systemctl disable -q --now "isumail-$dir" || true
  fi
done
"""


# isu-bench v1: the benchmarker of ISUMAIL v5.1 and 15 runs. Kept so that its results stay readable and reproducible.
class IsumailV1(Isumail):
    name = 'isumail-v1'
    title = 'ISUMAIL v1'
    amis = {**Isumail.amis, 'bench': 'ami-026c9e398c9d0e68e'}
    scored = False
    retired = True
    winner = 1_020_000
    initial = 2_153


PROBLEMS = {p.name: p for p in (Isucon14(), Isucon13(), Isumail(), IsumailV1())}
