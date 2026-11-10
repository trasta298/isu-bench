import datetime
import io
import json
import shlex
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

LIMIT = 11
# How long the benchmark host may stay unreachable before a run is given up as lost.
RECONNECT = 900


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Runner:
    # before/after return None or {'infra': reason, 'message': text, 'files': {name: text}}; an infra reason makes
    # the attempt uncounted.
    def __init__(self, out: Path, ssh_base: list, command, outcome, recover, recover_infra=None, before=None,
                 after=None, limit=LIMIT, infra_limit=None):
        self.out = out
        self.outcome = outcome
        self.ssh_base = ssh_base
        self.command = command
        self.recover = recover
        self.recover_infra = recover_infra or (lambda: None)
        self.before = before
        self.after = after
        self.limit = limit
        self.infra_limit = infra_limit
        self.lock = threading.Lock()
        self.runs = []
        self.infra_failures = []
        self.aborted = False
        self.finished = threading.Event()
        self.remote_dir = f'/tmp/isu-bench/{uuid.uuid4().hex}'
        out.mkdir(parents=True, exist_ok=True)

    def idle(self):
        if self.lock.acquire(blocking=False):
            self.lock.release()
            return True
        return False

    def hook(self, fn, attempt, stage, *args):
        res = fn(*args) if fn else None
        for name, text in ((res or {}).get('files') or {}).items():
            (self.out / f'attempt-{attempt:02d}.{stage}.{name}').write_text(text)
        return res or {}

    # command() is rebuilt on every try so that a retry resumes after what out already holds.
    def remote(self, command, out, send=None):
        lost = None
        while True:
            with (self.out / 'ssh.log').open('a') as log:
                proc = subprocess.Popen(self.ssh_base + [command()], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=log)
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    lost = None
                    if send:
                        send(line.decode(errors='replace'))
                code = proc.wait()
            if code != 255:
                return code
            lost = lost or time.monotonic()
            if time.monotonic() - lost > RECONNECT:
                return 255
            time.sleep(10)

    # The run is detached on the benchmark host, so a broken SSH session costs a reconnect instead of the run.
    def run_remote(self, command, attempt, stdout, stderr, send):
        d = f'{self.remote_dir}/{attempt:02d}'
        job = shlex.quote(f'echo $$ > pid.tmp && mv pid.tmp pid; ({command}); echo $? > exit.tmp && mv exit.tmp exit')
        start = (f'mkdir -p {self.remote_dir} && if mkdir {d}; then cd {d} || exit 1; '
                 f'setsid nohup bash -c {job} > stdout 2> stderr < /dev/null & fi')
        follow = 'for i in $(seq 50); do [ -e pid ] && break; sleep 0.2; done; tail -c +{} -f --pid=$(cat pid) stdout'
        status = io.BytesIO()
        steps = [(lambda: start, io.BytesIO(), None),
                 (lambda: f'cd {d} && ' + follow.format(stdout.tell() + 1), stdout, send),
                 (lambda: f'tail -c +{stderr.tell() + 1} {d}/stderr', stderr, None),
                 # A job killed before it could write its status reads as -1, like a crash of the benchmarker.
                 (lambda: f'{{ [ -e {d}/exit ] || echo -1 > {d}/exit; }} && tail -c +{status.tell() + 1} {d}/exit',
                  status, None)]
        for command, out, to in steps:
            if self.remote(command, out, to) != 0:
                return 255
        return int(status.getvalue())

    def not_counted(self, record, send, text):
        self.infra_failures.append(record)
        with (self.out / 'infra-failures.jsonl').open('a') as f:
            f.write(json.dumps(record) + '\n')
        if self.infra_limit is not None and len(self.infra_failures) > self.infra_limit:
            self.aborted = True
            send(f'{text} isu-bench: too many benchmarker failures; the session is stopped.\n')
            self.finished.set()
            return
        send(text)

    def execute(self, send):
        if not self.lock.acquire(blocking=False):
            send('isu-bench: another benchmark run is in progress\n')
            return
        try:
            n = len(self.runs) + 1
            if self.aborted:
                send('isu-bench: the session was stopped after too many benchmarker failures\n')
                return
            if n > self.limit:
                send(f'isu-bench: all {self.limit} benchmark runs have been used\n')
                return
            attempt = len(self.runs) + len(self.infra_failures) + 1
            record = {'n': n, 'attempt': attempt, 'started_at': now()}
            pre = self.hook(self.before, attempt, 'before', n, attempt)
            if pre.get('message'):
                send(pre['message'])
            if pre.get('infra'):
                record.update(finished_at=now(), stage='before', reason=pre['infra'])
                self.recover_infra()
                self.not_counted(record, send, 'isu-bench: the benchmark environment failed a check before the run, '
                                               f'not your application. This run was not counted. {self.limit - n + 1} runs left.\n')
                return
            send(f'isu-bench: starting benchmark run {n}/{self.limit}\n')
            command = self.command(attempt, self.runs) if callable(self.command) else self.command
            stdout_path, stderr_path = self.out / 'current.stdout', self.out / 'current.stderr'
            with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
                record['exit'] = self.run_remote(command, attempt, stdout, stderr, send)
            # 255 means the benchmark host was lost, which says nothing about the app.
            if record['exit'] == 255:
                stem = f'infra-{len(self.infra_failures) + 1:02d}'
                stdout_path.rename(self.out / f'{stem}.stdout')
                stderr_path.rename(self.out / f'{stem}.stderr')
                record.update(finished_at=now(), files=stem)
                self.not_counted(record, send, 'isu-bench: the benchmark host failed, not your application. Recovering it; '
                                               f'this run was not counted. {self.limit - n + 1} runs left.\n')
                self.recover()
                return
            result = self.outcome(stdout_path.read_text(), stderr_path.read_text())
            post = self.hook(self.after, attempt, 'after', n, attempt, result) if not result.get('infra') else {}
            reason = result.get('reason') if result.get('infra') else post.get('infra')
            if result.get('infra') or post.get('infra'):
                stem = f'infra-{len(self.infra_failures) + 1:02d}'
                stdout_path.rename(self.out / f'{stem}.stdout')
                stderr_path.rename(self.out / f'{stem}.stderr')
                record.update(finished_at=now(), files=stem, reason=reason or 'benchmarker error', outcome=result)
                self.recover_infra()
                self.not_counted(record, send, 'isu-bench: this run hit a failure of the benchmark environment, not your '
                                               f'application. It was not counted. {self.limit - n + 1} runs left.\n')
                return
            stdout_path = stdout_path.rename(self.out / f'{n:02d}.stdout')
            stderr_path = stderr_path.rename(self.out / f'{n:02d}.stderr')
            extra = {k: v for k, v in result.items() if k not in ('passed', 'score', 'infra')}
            record.update(finished_at=now(), passed=result['passed'], score=result['score'], **extra)
            self.runs.append(record)
            with (self.out / 'runs.jsonl').open('a') as f:
                f.write(json.dumps(record) + '\n')
            left = self.limit - n
            send(f'isu-bench: run {n}/{self.limit} finished: pass={str(record["passed"]).lower()} score={record["score"]}. '
                 f'{left} runs left.\n')
            if left == 0:
                self.finished.set()
        finally:
            self.lock.release()


class BenchServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, runner: Runner):
        self.runner = runner
        socket_path.unlink(missing_ok=True)
        super().__init__(str(socket_path), Handler)
        socket_path.chmod(0o666)

    @property
    def runs(self):
        return self.runner.runs

    @property
    def infra_failures(self):
        return self.runner.infra_failures

    @property
    def finished(self):
        return self.runner.finished

    def idle(self):
        return self.runner.idle()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()

        def send(text):
            try:
                self.wfile.write(text.encode())
                self.wfile.flush()
            except OSError:
                pass

        self.server.runner.execute(send)
