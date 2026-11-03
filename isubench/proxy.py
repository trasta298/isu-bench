#!/usr/bin/env python3
# Stdlib only: the same file runs on the benchmark host without installing anything.
import argparse
import asyncio
import json
import os
import ssl
import time
from urllib.parse import urlsplit


def allowed(rules, host, port):
    host = host.lower().rstrip('.')
    for rule in rules:
        if ':' in rule:
            if rule == f'{host}:{port}':
                return True
        elif port in (80, 443) and (host == rule or (rule.startswith('.') and host.endswith(rule))):
            return True
    return False


class Pacer:
    def __init__(self, rate):
        self.rate, self.next = rate, 0.0

    async def take(self, n):
        now = time.monotonic()
        start = max(self.next, now)
        self.next = start + n / self.rate
        await asyncio.sleep(start - now)


# Controls on the agent's SSH: delay and rate per host and direction, byte counts, and a file that closes it.
class Ssh:
    def __init__(self, port, delay, rate, closed_file, log):
        self.port, self.delay, self.rate, self.closed_file, self.log = port, delay, rate, closed_file, log
        self.pacers = {}
        self.open = {}
        self.aborted = 0
        self.reported = False

    def closed(self):
        return bool(self.closed_file) and os.path.exists(self.closed_file)

    def write(self, **record):
        self.log.write(json.dumps({'time': time.time(), **record}) + '\n')
        self.log.flush()

    async def pipe(self, reader, writer, tunnel, direction):
        pacer = self.pacers.setdefault((tunnel['host'], direction), Pacer(self.rate)) if self.rate else None
        # A bounded queue keeps backpressure on the sender, so the delay does not turn into unlimited buffering.
        queue = asyncio.Queue(maxsize=64)

        async def receive():
            try:
                while data := await reader.read(16384):
                    tunnel[direction] += len(data)
                    await queue.put((time.monotonic() + self.delay, data))
            except (ConnectionError, OSError):
                pass
            await queue.put((0, b''))

        async def send():
            try:
                while (item := await queue.get())[1]:
                    await asyncio.sleep(max(0, item[0] - time.monotonic()))
                    if pacer:
                        await pacer.take(len(item[1]))
                    writer.write(item[1])
                    await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()

        await asyncio.gather(receive(), send())

    async def tunnel(self, host, reader, writer, upstream_reader, upstream_writer):
        tunnel = {'host': host, 'up': 0, 'down': 0, 'opened': time.time(), 'writers': (writer, upstream_writer)}
        self.open[id(tunnel)] = tunnel
        try:
            await asyncio.gather(self.pipe(reader, upstream_writer, tunnel, 'up'),
                                 self.pipe(upstream_reader, writer, tunnel, 'down'))
        finally:
            self.open.pop(id(tunnel), None)
            self.write(event='ssh-bytes', host=host, up=tunnel['up'], down=tunnel['down'], opened=tunnel['opened'],
                       closed=True)

    async def watch(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            tunnels = list(self.open.values())
            if self.closed():
                self.aborted += len(tunnels)
                for t in tunnels:
                    for w in t['writers']:
                        w.transport.abort()
                if not self.reported and not self.open:
                    self.write(event='ssh-closed', open=0, aborted=self.aborted)
                    self.reported = True
            if time.monotonic() - last >= 10:
                last = time.monotonic()
                for t in tunnels:
                    self.write(event='ssh-bytes', host=t['host'], up=t['up'], down=t['down'], opened=t['opened'])


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(reader, writer, rules, log, ssh=None):
    try:
        head = await reader.readuntil(b'\r\n\r\n')
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    request_line, _, rest = head.partition(b'\r\n')
    try:
        method, target, version = request_line.decode('latin-1').split(' ')
        if method == 'CONNECT':
            host, port = target.rsplit(':', 1)
            host, port = host.strip('[]'), int(port)
        else:
            url = urlsplit(target)
            host, port = url.hostname or '', url.port or 80
            path = (url.path or '/') + (f'?{url.query}' if url.query else '')
            head = f'{method} {path} {version}\r\n'.encode('latin-1') + rest
    except ValueError:
        writer.close()
        return
    ok = allowed(rules, host, port)
    entry = {'time': time.time(), 'method': method, 'host': host, 'port': port, 'allowed': ok}
    if ok and ssh and port == ssh.port and ssh.closed():
        ok = entry['allowed'] = False
        entry['ssh_closed'] = True
    log.write(json.dumps(entry) + '\n')
    log.flush()
    if not ok:
        writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 30)
    except (OSError, asyncio.TimeoutError):
        writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    if method == 'CONNECT':
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        await writer.drain()
    else:
        upstream_writer.write(head)
    if ssh and method == 'CONNECT' and port == ssh.port:
        await ssh.tunnel(host, reader, writer, upstream_reader, upstream_writer)
        return
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def forward(reader, writer, host, token, log):
    try:
        head = await reader.readuntil(b'\r\n\r\n')
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    request_line, *headers = head.decode('latin-1').split('\r\n')
    headers = [h for h in headers if h and not h.lower().startswith(('host:', 'authorization:', 'x-api-key:', 'connection:'))]
    # One request per connection, so every request passes through here and gets the credential.
    head = '\r\n'.join([request_line, f'Host: {host}', f'Authorization: Bearer {token}', 'Connection: close',
                         *headers, '', '']).encode('latin-1')
    log.write(json.dumps({'time': time.time(), 'method': 'FORWARD', 'host': host, 'port': 443, 'allowed': True,
                          'path': request_line.split(' ')[1][:200] if ' ' in request_line else ''}) + '\n')
    log.flush()
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl.create_default_context()), 30)
    except (OSError, asyncio.TimeoutError):
        writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    upstream_writer.write(head)
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen', default='0.0.0.0:3128')
    parser.add_argument('--allow', required=True, help='file with one host, .domain or host:port per line')
    parser.add_argument('--log', required=True)
    parser.add_argument('--forward', help='LISTEN=HOST: plain HTTP in, HTTPS to HOST with a bearer token added')
    parser.add_argument('--bearer-file')
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--ssh-delay', type=float, default=0, help='one-way delay in seconds on CONNECT to the SSH port')
    parser.add_argument('--ssh-rate', type=float, default=0, help='bytes per second per host and direction on SSH')
    parser.add_argument('--ssh-closed', help='while this file exists, refuse and close CONNECTs to the SSH port')
    args = parser.parse_args()
    with open(args.allow) as f:
        rules = [line.strip().lower() for line in f if line.strip() and not line.startswith('#')]
    host, port = args.listen.rsplit(':', 1)
    with open(args.log, 'a') as log:
        ssh = (Ssh(args.ssh_port, args.ssh_delay, args.ssh_rate, args.ssh_closed, log)
               if args.ssh_delay or args.ssh_rate or args.ssh_closed else None)
        servers = [await asyncio.start_server(lambda r, w: handle(r, w, rules, log, ssh), host, int(port))]
        if args.forward:
            listen, upstream = args.forward.split('=')
            with open(args.bearer_file) as f:
                token = f.read().strip()
            forward_host, forward_port = listen.rsplit(':', 1)
            servers.append(await asyncio.start_server(lambda r, w: forward(r, w, upstream, token, log),
                                                      forward_host, int(forward_port)))
        await asyncio.gather(*(s.serve_forever() for s in servers), *([ssh.watch()] if ssh else []))


if __name__ == '__main__':
    asyncio.run(main())
