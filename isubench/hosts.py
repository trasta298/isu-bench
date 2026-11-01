import subprocess
import time
from pathlib import Path

APPS = ('s1', 's2', 's3')
TARGET_HOST = 'isuride.xiv.isucon.net'
PROXY_PORT = 3128


class Hosts:
    def __init__(self, work: Path, hosts: dict):
        self.work = work
        self.hosts = hosts
        self.config = work / 'ssh_config'

    def write_configs(self, harness_key: Path, agent_ssh: Path):
        entry = 'Host {name}\n  HostName {ip}\n  User {user}\n  IdentityFile {key}\n  IdentitiesOnly yes\n' \
                '  StrictHostKeyChecking accept-new\n  UserKnownHostsFile {known}\n  ServerAliveInterval 30\n{extra}\n'
        self.config.write_text(''.join(entry.format(name=n, ip=h['public'], user='ubuntu', key=harness_key,
                                                    known=self.work / 'known_hosts', extra='')
                                       for n, h in self.hosts.items()))
        (agent_ssh / 'config').write_text(''.join(
            entry.format(name=n, ip=self.hosts[n]['public'], user='isucon', key='~/.ssh/id_ed25519',
                         known='~/.ssh/known_hosts', extra=f'  ProxyCommand nc -X connect -x proxy:{PROXY_PORT} %h %p\n')
            for n in APPS))
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
        ip = self.hosts['s1']['private']
        proc = self.ssh('bench', f"curl -ks -o /dev/null -w '%{{http_code}}' --max-time 10 "
                                 f"--resolve {TARGET_HOST}:443:{ip} https://{TARGET_HOST}{path}", check=False)
        return int(proc.stdout or 0)

    def prepare(self, agent_public_key: str, lang: str, proxy_script: str, allowlist: str):
        for host in self.hosts:
            self.wait(host)
        self.wait('bench', 'grep -qw mem=9G /proc/cmdline')
        for host in self.hosts:
            self.ssh(host, 'cloud-init status --wait >/dev/null || true')
        # The AMI runs its own payment mock on 12345, where the benchmarker must listen instead.
        self.ssh('bench', "sudo systemctl disable --now nginx mysql $(systemctl list-unit-files --type=service "
                          "--no-legend --plain 'isuride*' | awk '{print $1}') && ! ss -ltn | grep -q ':12345 ' && "
                          "sudo test ! -e /home/isucon/.ssh/authorized_keys")
        self.ssh('bench', 'sudo install -d /opt/isu-bench && sudo tee /opt/isu-bench/proxy.py >/dev/null', input=proxy_script)
        self.ssh('bench', 'sudo tee /opt/isu-bench/allow.txt >/dev/null', input=allowlist)
        self.ssh('bench', f'sudo systemd-run --unit isu-bench-proxy -p Restart=always python3 /opt/isu-bench/proxy.py '
                          f'--listen 0.0.0.0:{PROXY_PORT} --allow /opt/isu-bench/allow.txt --log /var/log/isu-bench-proxy.jsonl')
        proxy = f"http://{self.hosts['bench']['private']}:{PROXY_PORT}"
        env = ''.join(f'{k}={proxy}\n' for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'))
        env += f'no_proxy=localhost,127.0.0.1,169.254.169.254,10.0.0.0/16,{TARGET_HOST}\nNO_PROXY=localhost,127.0.0.1,169.254.169.254,10.0.0.0/16,{TARGET_HOST}\n'
        apt = f'Acquire::http::Proxy "{proxy}";\nAcquire::https::Proxy "{proxy}";\n'
        switch = '' if lang == 'go' else (
            f'sudo systemctl disable --now isuride-go && sudo systemctl enable --now isuride-{lang} && ')
        if lang == 'php':
            switch += ('sudo ln -sf /etc/nginx/sites-available/isuride-php.conf /etc/nginx/sites-enabled/ && '
                       'sudo systemctl restart nginx && ')
        for host in APPS:
            self.ssh(host, f'{switch}sudo install -d -m 700 -o isucon -g isucon /home/isucon/.ssh && '
                           f"echo '{agent_public_key}' | sudo install -m 600 -o isucon -g isucon /dev/stdin "
                           '/home/isucon/.ssh/authorized_keys')
            self.ssh(host, 'sudo tee -a /etc/environment >/dev/null', input=env)
            self.ssh(host, 'sudo tee /etc/apt/apt.conf.d/99isu-bench-proxy >/dev/null', input=apt)
        if self.http_status('/') != 200:
            raise RuntimeError('s1 does not serve https after setup')
        return {h: self.ssh(h, "nproc; free -m | awk '/^Mem:/ {print $2}'").stdout.split() for h in self.hosts}

    def bench_command(self):
        return (f"sudo -iu isucon ./bench run --target https://{TARGET_HOST} --addr {self.hosts['s1']['private']}:443 "
                f"--payment-url http://{self.hosts['bench']['private']}:12345 -t 60")

    def reboot_check(self, infra):
        before = {h: self.boot_id(h) for h in APPS}
        infra.reboot(APPS)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            time.sleep(15)
            after = {h: self.boot_id(h) for h in APPS}
            if all(after[h] and after[h] != before[h] for h in APPS):
                break
        rebooted = {h: bool(after[h] and after[h] != before[h]) for h in APPS}
        deadline = time.monotonic() + 600
        while True:
            root, api = self.http_status('/'), self.http_status('/api/app/rides')
            ok = root == 200 and 200 <= api < 500
            if ok or time.monotonic() > deadline:
                return {'passed': ok, 'rebooted': rebooted, 'root_status': root, 'api_status': api}
            time.sleep(15)

    def proxy_log(self):
        return self.ssh('bench', 'sudo cat /var/log/isu-bench-proxy.jsonl', check=False).stdout
