import re
import subprocess
from pathlib import Path

LANGS = {'go': 'Go', 'rust': 'Rust', 'node': 'Node.js', 'perl': 'Perl', 'php': 'PHP', 'python': 'Python', 'ruby': 'Ruby'}
CONTEST_TYPES = {'s1': 'c5.large', 's2': 'c5.large', 's3': 'c5.large', 'bench': 'c5.2xlarge'}


class Isucon14:
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

    def prepare_apps(self, h, work):
        pass

    def bench_command(self, hosts):
        return (f"sudo -iu isucon ./bench run --target https://{self.target_host} --addr {hosts['s1']['private']}:443 "
                f"--payment-url http://{hosts['bench']['private']}:12345 -t 60")

    def parse(self, stdout, stderr):
        match = self.result.findall(stdout)
        return (bool(match) and match[-1][0] == 'true', int(match[-1][1]) if match else None)


class Isucon13:
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


PROBLEMS = {p.name: p for p in (Isucon14(), Isucon13())}
