"""Shared test isolation for provider selection and outbound network access."""
from __future__ import annotations

import ipaddress
import os
import socket

import pytest


# Do not let the repository's developer .env choose a live provider for tests.
# LLM call sites in tests should patch the selected adapter with a fake.
os.environ["LLM_PROVIDER"] = "openrouter"


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if str(host).casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_external_test_network(monkeypatch):
    """Allow local fake servers, but fail before any external socket connects."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        if not _is_loopback(address):
            raise AssertionError(f"Test attempted an external network connection to {address!r}")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_loopback(address):
            raise AssertionError(f"Test attempted an external network connection to {address!r}")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
