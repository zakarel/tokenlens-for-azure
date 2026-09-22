"""Shared guards for the test suite.

Two invariants are enforced for every test, not per test file:

* no test may open a network connection — TokenLens analysis is offline, and
  pricing synchronization is only ever exercised through injected transports
  and committed synthetic fixtures;
* no test may read or write the developer's real user-local configuration.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest


class NetworkAccessDenied(RuntimeError):
    """A test attempted real network I/O."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*args, **kwargs):  # noqa: ANN002, ANN003 - signature mirrors the stdlib
        raise NetworkAccessDenied(
            "The TokenLens test suite never performs network I/O. Use a synthetic fixture "
            "and an injected transport instead."
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path_factory, monkeypatch):
    """Point the user-local config directory at a per-test temporary path."""
    monkeypatch.setenv(
        "TOKENLENS_CONFIG_DIR", str(tmp_path_factory.mktemp("tokenlens-user-config"))
    )
    # The guided workflow's opportunistic pricing sync is opt-in for tests that
    # exercise it explicitly with an injected transport.
    monkeypatch.setenv("TOKENLENS_NO_PRICING_SYNC", "1")
    yield
