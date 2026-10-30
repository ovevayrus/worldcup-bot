"""Run offline tests with external networking blocked and a synthetic environment."""

import ipaddress
import os
import socket
import sys
import unittest
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
test_home = repo / ".test-tmp"
test_home.mkdir(exist_ok=True)
keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "NUMBER_OF_PROCESSORS"}
for key in list(os.environ):
    if key.upper() not in keep:
        del os.environ[key]
os.environ.update(
    USERPROFILE=str(test_home),
    HOME=str(test_home),
    APPDATA=str(test_home),
    LOCALAPPDATA=str(test_home),
    TEMP=str(test_home),
    TMP=str(test_home),
    PYTHONNOUSERSITE="1",
)
connect = socket.socket.connect
connect_ex = socket.socket.connect_ex
getaddrinfo = socket.getaddrinfo


def loopback(address):
    if not isinstance(address, tuple):
        return True
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        return str(address[0]).lower() == "localhost"


def guarded_connect(self, address):
    if not loopback(address):
        raise RuntimeError("External networking is disabled in unit tests.")
    return connect(self, address)


def guarded_connect_ex(self, address):
    if not loopback(address):
        raise RuntimeError("External networking is disabled in unit tests.")
    return connect_ex(self, address)


def guarded_dns(host, *args, **kwargs):
    if host is not None and not loopback((host, 0)):
        raise RuntimeError("External DNS is disabled in unit tests.")
    return getaddrinfo(host, *args, **kwargs)


socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
socket.getaddrinfo = guarded_dns
os.chdir(repo)
sys.path[:0] = [str(repo), str(repo / "tests")]
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.discover(str(repo / "tests"))
)
raise SystemExit(not result.wasSuccessful())
