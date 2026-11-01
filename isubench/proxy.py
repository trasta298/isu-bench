#!/usr/bin/env python3
# Stdlib only: the same file runs on the benchmark host without installing anything.
import argparse
import asyncio
import json
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


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(reader, writer, rules, log):
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
    log.write(json.dumps({'time': time.time(), 'method': method, 'host': host, 'port': port, 'allowed': ok}) + '\n')
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
    args = parser.parse_args()
    with open(args.allow) as f:
        rules = [line.strip().lower() for line in f if line.strip() and not line.startswith('#')]
    host, port = args.listen.rsplit(':', 1)
    with open(args.log, 'a') as log:
        servers = [await asyncio.start_server(lambda r, w: handle(r, w, rules, log), host, int(port))]
        if args.forward:
            listen, upstream = args.forward.split('=')
            with open(args.bearer_file) as f:
                token = f.read().strip()
            forward_host, forward_port = listen.rsplit(':', 1)
            servers.append(await asyncio.start_server(lambda r, w: forward(r, w, upstream, token, log),
                                                      forward_host, int(forward_port)))
        await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == '__main__':
    asyncio.run(main())
