import ipaddress
import socket
import struct
import threading
from unittest.mock import patch

import pytest

from telegram_egress import TelegramProxy, read_exact, sandbox_profile, target_allowed


@pytest.mark.parametrize('host,port', [
    ('149.154.167.51', 443), ('91.108.4.1', 443), ('2001:b28:f23d::1', 443),
])
def test_allows_official_numeric_telegram_endpoints(host, port):
    assert target_allowed(host, port)


@pytest.mark.parametrize('host,port', [
    ('149.154.167.51', 80), ('149.154.167.51', 444), ('149.154.159.255', 443),
    ('149.154.176.0', 443), ('1.1.1.1', 443), ('127.0.0.1', 443),
    ('::1', 443), ('::ffff:149.154.167.51', 443), ('api.telegram.org', 443),
    ('example.com', 443), ('149.154.167.51.example.com', 443),
])
def test_disallows_every_other_target(host, port):
    assert not target_allowed(host, port)


def exercise(request, expected_code):
    client, server = socket.socketpair()
    client.settimeout(2)
    proxy = TelegramProxy()
    thread = threading.Thread(target=proxy.handle, args=(server,))
    with patch('telegram_egress.dial_numeric') as dial:
        thread.start()
        client.sendall(b'\x05\x01\x00')
        assert read_exact(client, 2) == b'\x05\x00'
        client.sendall(request)
        assert read_exact(client, 10)[1] == expected_code
        thread.join(timeout=2)
        assert not thread.is_alive()
        dial.assert_not_called()
    client.close()
    server.close()


def test_rejects_domain_before_connect():
    exercise(b'\x05\x01\x00\x03', 8)


def test_rejects_nontelegram_before_connect():
    exercise(b'\x05\x01\x00\x01' + socket.inet_aton('1.1.1.1') + struct.pack('!H', 443), 2)


def test_rejects_port_before_connect():
    exercise(b'\x05\x01\x00\x01' + socket.inet_aton('149.154.167.51') + struct.pack('!H', 80), 2)


def test_rejects_bind_command():
    exercise(b'\x05\x02\x00\x01', 7)


def test_relays_only_after_valid_destination():
    client, server = socket.socketpair()
    upstream, peer = socket.socketpair()
    for stream in (client, peer):
        stream.settimeout(2)
    proxy = TelegramProxy()
    with patch('telegram_egress.dial_numeric', return_value=upstream) as dial:
        thread = threading.Thread(target=proxy.handle, args=(server,))
        thread.start()
        client.sendall(b'\x05\x01\x00')
        assert read_exact(client, 2) == b'\x05\x00'
        client.sendall(b'\x05\x01\x00\x01' + socket.inet_aton('149.154.167.51') + struct.pack('!H', 443))
        assert read_exact(client, 10)[1] == 0
        client.sendall(b'outbound synthetic bytes')
        assert read_exact(peer, 24) == b'outbound synthetic bytes'
        peer.sendall(b'inbound bytes')
        assert read_exact(client, 13) == b'inbound bytes'
        dial.assert_called_once_with('149.154.167.51', 443)
        client.shutdown(socket.SHUT_RDWR)
        thread.join(timeout=2)
        assert not thread.is_alive()
    for stream in (client, server, peer):
        stream.close()


def test_profile_cannot_inject_port():
    with pytest.raises(ValueError):
        sandbox_profile('80"))(allow network*)')
