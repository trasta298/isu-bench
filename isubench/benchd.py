import datetime
import json
import re
import socketserver
import subprocess
import threading
from pathlib import Path

LIMIT = 15
RESULT = re.compile(r'msg=結果 pass=(true|false) スコア=(-?\d+)')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class BenchServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, out: Path, ssh_base: list, command: str, recover):
        self.out = out
        self.ssh_base = ssh_base
        self.command = command
        self.recover = recover
        self.lock = threading.Lock()
        self.runs = []
        self.infra_failures = []
        self.finished = threading.Event()
        out.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        super().__init__(str(socket_path), Handler)
        socket_path.chmod(0o666)

    def idle(self):
        if self.lock.acquire(blocking=False):
            self.lock.release()
            return True
        return False

    def execute(self, send):
        if not self.lock.acquire(blocking=False):
            send('isu-bench: another benchmark run is in progress\n')
            return
        try:
            n = len(self.runs) + 1
            if n > LIMIT:
                send(f'isu-bench: all {LIMIT} benchmark runs have been used\n')
                return
            record = {'n': n, 'started_at': now()}
            send(f'isu-bench: starting benchmark run {n}/{LIMIT}\n')
            stdout_path, stderr_path = self.out / 'current.stdout', self.out / 'current.stderr'
            with stdout_path.open('w') as stdout, stderr_path.open('w') as stderr:
                proc = subprocess.Popen(self.ssh_base + [self.command], stdout=subprocess.PIPE, stderr=stderr, text=True)
                for line in proc.stdout:
                    stdout.write(line)
                    stdout.flush()
                    send(line)
                record['exit'] = proc.wait()
            # 255 is ssh's own failure: the benchmark host was lost, which says nothing about the app.
            if record['exit'] == 255:
                stem = f'infra-{len(self.infra_failures) + 1:02d}'
                stdout_path.rename(self.out / f'{stem}.stdout')
                stderr_path.rename(self.out / f'{stem}.stderr')
                record.update(finished_at=now(), files=stem)
                self.infra_failures.append(record)
                with (self.out / 'infra-failures.jsonl').open('a') as f:
                    f.write(json.dumps(record) + '\n')
                send('isu-bench: the benchmark host failed, not your application. Recovering it; '
                     f'this run was not counted. {LIMIT - n + 1} runs left.\n')
                self.recover()
                return
            stdout_path = stdout_path.rename(self.out / f'{n:02d}.stdout')
            stderr_path.rename(self.out / f'{n:02d}.stderr')
            match = RESULT.findall(stdout_path.read_text())
            record.update(finished_at=now(), passed=bool(match) and match[-1][0] == 'true',
                          score=int(match[-1][1]) if match else None)
            self.runs.append(record)
            with (self.out / 'runs.jsonl').open('a') as f:
                f.write(json.dumps(record) + '\n')
            left = LIMIT - n
            send(f'isu-bench: run {n}/{LIMIT} finished: pass={str(record["passed"]).lower()} score={record["score"]}. '
                 f'{left} runs left.\n')
            if left == 0:
                self.finished.set()
        finally:
            self.lock.release()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()

        def send(text):
            try:
                self.wfile.write(text.encode())
                self.wfile.flush()
            except OSError:
                pass

        self.server.execute(send)
