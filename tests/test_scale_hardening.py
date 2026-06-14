"""
test_scale_hardening.py — multi-worker / behind-a-proxy correctness.

Under gunicorn -w 4 behind nginx, three things must hold:
  1. The per-key gateway RPM cap (_rpm_check) actually enforces the limit and is
     keyed per-account — and it rides shared storage (here, an in-memory strategy)
     so it can be made global with redis. Fail-open on a storage error.
  2. ProxyFix wires up ONLY when TRUST_PROXY > 0, and when it does, X-Forwarded-For
     drives request.remote_addr (the rate-limit key + audit IP) instead of the
     proxy's address — and is IGNORED when TRUST_PROXY is unset (anti-spoofing).
  3. config.trust_proxy_hops() parses the env safely.

Network-free, redis-free (uses limits' in-memory storage). Run from project root:

    .venv/bin/python -m pytest tests/test_scale_hardening.py -v
"""

import os
import sys

import pytest
from flask import Flask, request
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — one-time, idempotent module boot
from agent_team import config  # noqa: E402


@pytest.fixture
def fresh_rpm(monkeypatch):
    """Give _rpm_check a private in-memory window so the test is hermetic regardless
    of the ambient RATELIMIT_STORAGE_URI."""
    monkeypatch.setattr(app, "_RPM_STRATEGY", MovingWindowRateLimiter(MemoryStorage()))


# ---------------------------------------------------------------------------
# 1. Gateway per-key RPM
# ---------------------------------------------------------------------------
def test_rpm_check_enforces_limit(fresh_rpm):
    assert app._rpm_check("acct-1", 3) is True
    assert app._rpm_check("acct-1", 3) is True
    assert app._rpm_check("acct-1", 3) is True
    assert app._rpm_check("acct-1", 3) is False  # 4th in the window is over the cap


def test_rpm_check_is_per_key(fresh_rpm):
    for _ in range(3):
        assert app._rpm_check("acct-A", 3) is True
    assert app._rpm_check("acct-A", 3) is False
    # a different key has its own independent budget
    assert app._rpm_check("acct-B", 3) is True


def test_rpm_check_zero_or_none_disables(fresh_rpm):
    for _ in range(100):
        assert app._rpm_check("acct-unlimited", 0) is True
    assert app._rpm_check("acct-unlimited", None) is True


def test_rpm_check_fails_open_on_storage_error(monkeypatch):
    class _Boom:
        def hit(self, *a, **k):
            raise RuntimeError("redis down")
    monkeypatch.setattr(app, "_RPM_STRATEGY", _Boom())
    # A backend hiccup must ALLOW the request, never 500 the gateway.
    assert app._rpm_check("acct-x", 5) is True


# ---------------------------------------------------------------------------
# 2. ProxyFix — trust X-Forwarded-For only when TRUST_PROXY > 0
# ---------------------------------------------------------------------------
def _echo_remote_addr_app():
    a = Flask(__name__)

    @a.route("/ip")
    def _ip():
        return request.remote_addr or ""

    return a


def test_proxyfix_trusts_xff_when_hops_set(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    a = _echo_remote_addr_app()
    assert app._install_proxy_fix(a) is True
    r = a.test_client().get("/ip", headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.get_data(as_text=True) == "1.2.3.4"


def test_proxyfix_ignores_xff_when_unset(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    a = _echo_remote_addr_app()
    assert app._install_proxy_fix(a) is False
    r = a.test_client().get("/ip", headers={"X-Forwarded-For": "1.2.3.4"})
    # XFF is NOT trusted → the direct client address stands (anti-spoofing).
    assert r.get_data(as_text=True) == "127.0.0.1"


# ---------------------------------------------------------------------------
# 3. config.trust_proxy_hops() parsing
# ---------------------------------------------------------------------------
def test_trust_proxy_hops_parsing(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert config.trust_proxy_hops() == 0
    monkeypatch.setenv("TRUST_PROXY", "1")
    assert config.trust_proxy_hops() == 1
    monkeypatch.setenv("TRUST_PROXY", "2")
    assert config.trust_proxy_hops() == 2
    monkeypatch.setenv("TRUST_PROXY", "not-a-number")
    assert config.trust_proxy_hops() == 0
    monkeypatch.setenv("TRUST_PROXY", "-3")
    assert config.trust_proxy_hops() == 0
