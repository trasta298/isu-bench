import subprocess
import time
from pathlib import Path

APPS = ('s1', 's2', 's3')
PROXY_PORT = 3128


class Hosts:
    def __init__(self, work: Path, hosts: dict, problem):
        self.work = work
        self.hosts = hosts
        self.problem = problem
        self.config = work / 'ssh_config'

    def write_configs(self, harness_key: Path, agent_ssh: Path):
        entry = 'Host {name}\n  HostName {ip}\n  User {user}\n  IdentityFile {key}\n  IdentitiesOnly yes\n' \
                '  StrictHostKeyChecking accept-new\n  UserKnownHostsFile {known}\n  ServerAliveInterval 30\n{extra}\n'
        jump = self.problem.harness_jump
        # Through the jump host the harness keeps reaching the apps after the agent's SSH rule is removed.
        via = lambda n: (jump and n in self.problem.apps and n != jump)
        self.config.write_text(''.join(entry.format(name=n, ip=h['private'] if via(n) else h['public'], user='ubuntu',
                                                    key=harness_key, known=self.work / 'known_hosts',
                                                    extra=f'  ProxyJump {jump}\n' if via(n) else '')
                                       for n, h in self.hosts.items()))
        (agent_ssh / 'config').write_text(''.join(
            entry.format(name=n, ip=self.hosts[n]['public'], user='isucon', key='~/.ssh/id_ed25519',
                         known='~/.ssh/known_hosts', extra=f'  ProxyCommand nc -X connect -x proxy:{PROXY_PORT} %h %p\n')
            for n in self.problem.apps))
        for f in (self.config, agent_ssh / 'config'):
            f.chmod(0o600)

    def ssh(self, host, command, timeout=600, check=True, input=None):
        proc = subprocess.run(['ssh', '-F', str(self.config), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                               host, command], capture_output=True, text=True, timeout=timeout, input=input)
        if check and proc.returncode:
            raise RuntimeError(f'ssh {host} failed ({proc.returncode}): {proc.stderr.strip()}')
        return proc

    def wait(self, host, probe='true', limit=900):
        deadline = time.monotonic() + limit
        while True:
            try:
                if self.ssh(host, probe, timeout=30, check=False).returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f'{host} did not become ready')
            time.sleep(10)

    def boot_id(self, host):
        try:
            proc = self.ssh(host, 'cat /proc/sys/kernel/random/boot_id', timeout=30, check=False)
            return proc.stdout.strip() if proc.returncode == 0 else None
        except subprocess.TimeoutExpired:
            return None

    def http_status(self, path):
        ip, target = self.hosts['s1']['private'], self.problem.target_host
        proc = self.ssh('bench', f"curl -ks -o /dev/null -w '%{{http_code}}' --max-time 10 "
                                 f"--resolve {target}:443:{ip} https://{target}{path}", check=False)
        return int(proc.stdout or 0)

    def prepare(self, agent_public_key: str, lang: str, proxy_script: str, allowlist: str):
        for host in self.hosts:
            self.wait(host)
        for host in self.hosts:
            self.ssh(host, 'cloud-init status --wait >/dev/null || true')
        self.problem.prepare_bench(self, self.work)
        self.ssh('bench', f'{self.problem.bench_ready} && sudo test ! -e /home/isucon/.ssh/authorized_keys')
        if hasattr(self.problem, 'prepare_router'):
            self.problem.prepare_router(self)
        self.problem.prepare_apps(self, self.work)
        self.ssh('bench', 'sudo install -d /opt/isu-bench && sudo tee /opt/isu-bench/proxy.py >/dev/null', input=proxy_script)
        self.ssh('bench', 'sudo tee /opt/isu-bench/allow.txt >/dev/null', input=allowlist)
        unit = ('[Unit]\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /opt/isu-bench/proxy.py '
                f'--listen 0.0.0.0:{PROXY_PORT} --allow /opt/isu-bench/allow.txt --log /var/log/isu-bench-proxy.jsonl\n'
                'Restart=always\n[Install]\nWantedBy=multi-user.target\n')
        self.ssh('bench', 'sudo tee /etc/systemd/system/isu-bench-proxy.service >/dev/null && '
                          'sudo systemctl enable --now isu-bench-proxy', input=unit)
        proxy = f"http://{self.hosts['bench']['private']}:{PROXY_PORT}"
        env = ''.join(f'{k}={proxy}\n' for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'))
        no_proxy = ','.join(['localhost', '127.0.0.1', '169.254.169.254', '10.0.0.0/16', *self.problem.no_proxy_hosts])
        env += f'no_proxy={no_proxy}\nNO_PROXY={no_proxy}\n'
        apt = f'Acquire::http::Proxy "{proxy}";\nAcquire::https::Proxy "{proxy}";\n'
        svc = self.problem.service
        switch = '' if lang == 'go' else (
            f'sudo systemctl disable --now {svc}-go && sudo systemctl enable --now {svc}-{lang} && ')
        if lang == 'php':
            switch += (f'sudo ln -sf /etc/nginx/sites-available/{svc}-php.conf /etc/nginx/sites-enabled/ && '
                       'sudo systemctl restart nginx && ')
        for host in self.problem.apps:
            self.ssh(host, f'{switch}sudo install -d -m 700 -o isucon -g isucon /home/isucon/.ssh && '
                           f"echo '{agent_public_key}' | sudo install -m 600 -o isucon -g isucon /dev/stdin "
                           '/home/isucon/.ssh/authorized_keys')
            self.ssh(host, 'sudo tee -a /etc/environment >/dev/null', input=env)
            self.ssh(host, 'sudo tee /etc/apt/apt.conf.d/99isu-bench-proxy >/dev/null', input=apt)
        self.problem.check_ready(self)
        return {h: self.ssh(h, "nproc; free -m | awk '/^Mem:/ {print $2}'; lscpu | sed -n 's/^Model name: *//p' | head -1")
                .stdout.splitlines() for h in self.hosts}

    def recover_bench(self, infra):
        infra.reboot(['bench'])
        time.sleep(30)
        self.wait('bench', f'systemctl is-active -q isu-bench-proxy && {self.problem.bench_ready}')

    def reboot_apps(self, infra):
        apps = self.problem.apps
        before = {h: self.boot_id(h) for h in apps}
        infra.reboot(apps)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            time.sleep(15)
            after = {h: self.boot_id(h) for h in apps}
            if all(after[h] and after[h] != before[h] for h in apps):
                break
        return {h: bool(after[h] and after[h] != before[h]) for h in apps}

    def reboot_check(self, infra, last=None, out=None):
        rebooted = self.reboot_apps(infra)
        if hasattr(self.problem, 'verify_reboot'):
            return self.problem.verify_reboot(self, rebooted, last, out)
        deadline = time.monotonic() + 600
        while True:
            root, api = (self.http_status(p) for p in self.problem.probe_paths)
            ok = root == 200 and 200 <= api < 500
            if ok or time.monotonic() > deadline:
                return {'passed': ok, 'rebooted': rebooted, 'root_status': root, 'api_status': api}
            time.sleep(15)

    def proxy_log(self):
        return self.ssh('bench', 'sudo cat /var/log/isu-bench-proxy.jsonl', check=False).stdout
