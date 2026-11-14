"""Local numeric-only Telegram egress proxy; never inspects MTProto payloads."""
from __future__ import annotations

import ipaddress
import os
import select
import socket
import struct
import subprocess
import threading
from collections.abc import Mapping, Sequence

# https://core.telegram.org/resources/cidr.txt, verified 2026-11-14.
TELEGRAM_NETWORKS = tuple(map(ipaddress.ip_network, (
    '91.108.56.0/22', '91.108.4.0/22', '91.108.8.0/22',
    '91.108.16.0/22', '91.108.12.0/22', '149.154.160.0/20',
    '91.105.192.0/23', '91.108.20.0/22', '185.76.151.0/24',
    '2001:b28:f23d::/48', '2001:b28:f23f::/48', '2001:67c:4e8::/48',
    '2001:b28:f23c::/48', '2a0a:f280::/32',
)))


def target_allowed(host: str, port: int) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return port == 443 and any(address in network for network in TELEGRAM_NETWORKS)


def read_exact(stream: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = stream.recv(count - len(result))
        if not chunk:
            raise EOFError('Incomplete SOCKS request')
        result.extend(chunk)
    return bytes(result)


def dial_numeric(host: str, port: int) -> socket.socket:
    if not target_allowed(host, port):
        raise PermissionError('Egress destination denied')
    address = ipaddress.ip_address(host)
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    stream = socket.socket(family, socket.SOCK_STREAM)
    stream.settimeout(15)
    try:
        endpoint = (str(address), port) if address.version == 4 else (str(address), port, 0, 0)
        stream.connect(endpoint)
        return stream
    except BaseException:
        stream.close()
        raise


def socks_reply(stream: socket.socket, status: int) -> None:
    stream.sendall(bytes((5, status, 0, 1)) + bytes(6))


class TelegramProxy:
    """Bound to IPv4 loopback; all nonnumeric or non-Telegram targets are denied."""

    def __init__(self):
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(16)
        self._listeners: list[socket.socket] = []
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def __enter__(self):
        ipv4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ipv6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self._listeners = [ipv4, ipv6]
        try:
            ipv4.bind(('127.0.0.1', 0))
            self.port = ipv4.getsockname()[1]
            ipv6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            ipv6.bind(('::1', self.port))
            for listener in self._listeners:
                listener.listen(16)
                listener.settimeout(0.5)
        except BaseException:
            for listener in self._listeners:
                listener.close()
            raise
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        for listener in self._listeners:
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _accept(self):
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select(self._listeners, [], [], 0.5)
                for listener in readable:
                    stream, _ = listener.accept()
                    if not self._slots.acquire(blocking=False):
                        stream.close()
                        continue
                    threading.Thread(target=self._handle_guarded, args=(stream,), daemon=True).start()
            except (OSError, ValueError):
                return

    def _handle_guarded(self, stream):
        try:
            self.handle(stream)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            stream.close()
            self._slots.release()

    def handle(self, stream: socket.socket):
        stream.settimeout(10)
        version, method_count = read_exact(stream, 2)
        if version != 5 or not method_count:
            return
        methods = read_exact(stream, method_count)
        if 0 not in methods:
            stream.sendall(b'\x05\xff')
            return
        stream.sendall(b'\x05\x00')
        version, command, reserved, kind = read_exact(stream, 4)
        if version != 5 or command != 1 or reserved != 0:
            socks_reply(stream, 7)
            return
        if kind == 1:
            host = socket.inet_ntop(socket.AF_INET, read_exact(stream, 4))
        elif kind == 4:
            host = socket.inet_ntop(socket.AF_INET6, read_exact(stream, 16))
        else:
            socks_reply(stream, 8)
            return
        port = struct.unpack('!H', read_exact(stream, 2))[0]
        if not target_allowed(host, port):
            socks_reply(stream, 2)
            return
        try:
            upstream = dial_numeric(host, port)
        except OSError:
            socks_reply(stream, 5)
            return
        with upstream:
            socks_reply(stream, 0)
            stream.settimeout(30)
            upstream.settimeout(30)
            while not self._stop.is_set():
                readable, _, _ = select.select([stream, upstream], [], [], 1)
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    destination = upstream if source is stream else stream
                    destination.sendall(payload)


def sandbox_profile(port: int) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('Invalid proxy port')
    return '\n'.join((
        '(version 1)',
        '(allow default)',
        '(deny network*)',
        f'(allow network-outbound (remote tcp "localhost:{port}"))',
        '(deny file-write*)',
        '(deny file-read* (subpath \"/Users\"))',
        '(deny mach-lookup)',
    ))


def run_sandboxed_child(command: Sequence[str], environment: Mapping[str, str], cwd: str) -> int:
    """Called only by the fixed, root-owned launcher; never accept MCP parameters."""
    with TelegramProxy() as proxy:
        env = dict(environment)
        env['TG_LOCAL_PROXY_PORT'] = str(proxy.port)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        profile = sandbox_profile(proxy.port)
        child = subprocess.Popen(
            ['/usr/bin/sandbox-exec', '-p', profile, *command],
            env=env, cwd=cwd, close_fds=True,
        )
        try:
            return child.wait()
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
