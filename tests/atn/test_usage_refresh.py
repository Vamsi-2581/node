"""Subscription-usage refresh goes through the bridges' sanctioned get_usage
request (Agent SDK control request / codex app-server), never a direct
vendor API call with the user's OAuth token. Throttled, and any failure
falls back to the cached stream-event data."""
from __future__ import annotations

import asyncio

import pytest

from atn.providers import bridge as bridge_mod
from atn.providers import codex_bridge as codex_mod
from atn.providers.bridge import BridgeProvider
from atn.providers.codex_bridge import CodexBridgeProvider


def _claude_stub(resp):
    s = BridgeProvider.__new__(BridgeProvider)
    s._lock = asyncio.Lock()
    s._rate_limits = {}
    s._cum_tokens_by_class = {"haiku": 0, "sonnet": 0, "opus": 0, "other": 0}
    s._last_snapshot = None
    s.sent = []

    async def _send(req):
        s.sent.append(req)
        if isinstance(resp, Exception):
            raise resp
        return resp
    s._send_request = _send
    return s


def _codex_stub(resp):
    s = CodexBridgeProvider.__new__(CodexBridgeProvider)
    s._lock = asyncio.Lock()
    s._rate_limits = {}
    s._usage_fetched_at = 0.0
    s.sent = []

    async def _send(req):
        s.sent.append(req)
        return resp
    s._send_request = _send
    return s


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    monkeypatch.setattr(BridgeProvider, "_shared_usage_fetched_at", 0.0)


_OK = {"ok": True, "available": True, "rate_limits": {
    "five_hour": {"status": "allowed", "rateLimitType": "five_hour",
                  "utilization": 0.04, "resetsAt": 1791852000, "updatedAt": 1},
}}


@pytest.mark.asyncio
async def test_claude_refresh_uses_bridge_get_usage():
    s = _claude_stub(_OK)
    out = await s.refresh_usage()
    assert s.sent == [{"type": "get_usage"}]
    assert out["five_hour"]["utilization"] == 0.04
    assert out["five_hour"]["resetsAt"] == 1791852000


@pytest.mark.asyncio
async def test_claude_refresh_throttled():
    s = _claude_stub(_OK)
    await s.refresh_usage()
    await s.refresh_usage()
    assert len(s.sent) == 1


@pytest.mark.asyncio
async def test_claude_refresh_skipped_while_busy():
    s = _claude_stub(_OK)
    s._rate_limits["five_hour"] = {"utilization": 0.5}
    async with s._lock:
        out = await s.refresh_usage()
    assert s.sent == []
    assert out["five_hour"]["utilization"] == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("resp", [
    {"ok": True, "available": False, "reason": "sdk has no usage method"},
    RuntimeError("bridge died"),
])
async def test_claude_refresh_falls_back_to_cache(resp):
    s = _claude_stub(resp)
    s._rate_limits["five_hour"] = {"utilization": 0.3, "resetsAt": 5}
    out = await s.refresh_usage()
    assert out == {"five_hour": {"utilization": 0.3, "resetsAt": 5}}


@pytest.mark.asyncio
async def test_reconcile_skips_when_no_fresh_reading():
    s = _claude_stub({"ok": True, "available": False})
    s._predicted_pct_since_refresh = 1.5
    s._predicted_tokens_by_class_since_refresh = {"sonnet": 10}
    report = await s.reconcile_after_agent_loop()
    assert report["skipped"] is True
    # Prediction keeps accumulating until a real reading arrives.
    assert s._predicted_pct_since_refresh == 1.5


@pytest.mark.asyncio
async def test_codex_refresh_uses_bridge_get_usage():
    s = _codex_stub({"ok": True, "available": True, "rate_limits": {
        "30d": {"status": "allowed", "rateLimitType": "30d", "utilization": 0.0,
                "resetsAt": 1794426515, "windowSeconds": 2592000}}})
    out = await s.refresh_usage()
    assert s.sent == [{"type": "get_usage"}]
    assert out["30d"]["windowSeconds"] == 2592000
    await s.refresh_usage()
    assert len(s.sent) == 1  # throttled


def test_no_direct_token_reads_in_providers():
    import inspect
    for mod in (bridge_mod, codex_mod):
        src = inspect.getsource(mod)
        for needle in (".credentials.json", "auth.json", "api.anthropic.com",
                       "backend-api", "Bearer"):
            assert needle not in src, (mod.__name__, needle)
