"""Generic-loop techniques ported (as ideas) from Codex CLI (docs/agentic_loop.md §17-§24).

Each section maps to one item of the port:
  §17 non-blocking shell tools (async subprocess, tree kill, head+tail)
  §18 plain-text tool results
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import pytest

from atn import shell_tools
from atn.providers.base import (
    Provider,
    ProviderResponse,
    ToolCall,
    Usage,
    format_tool_result,
)
from atn.shell_tools import exec_bash, exec_search_files, _head_tail


PY = f'"{sys.executable}"'


# ---------------------------------------------------------------------------
# §17 Non-blocking shell tools
# ---------------------------------------------------------------------------

class TestNonBlockingShell:
    async def test_bash_does_not_block_event_loop(self):
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        tk = asyncio.create_task(ticker())
        try:
            r = await exec_bash({"command": f'{PY} -c "import time; time.sleep(1); print(42)"'})
        finally:
            tk.cancel()
        assert r["exit_code"] == 0
        assert "42" in r["output"]
        # A blocking subprocess.run would leave the ticker at ~0.
        assert ticks >= 8

    async def test_bash_merges_stderr_and_reports_exit(self):
        r = await exec_bash({"command": f'{PY} -c "import sys; print(1); sys.stderr.write(\'boom\'); sys.exit(3)"'})
        assert r["exit_code"] == 3
        assert "1" in r["output"] and "boom" in r["output"]

    async def test_bash_timeout_kills_and_returns_partial_output(self):
        t0 = time.monotonic()
        r = await exec_bash({
            "command": f'{PY} -c "import time; print(\'started\', flush=True); time.sleep(60)"',
            "timeout": 1,
        })
        assert time.monotonic() - t0 < 20
        assert "timed out" in r["error"]
        assert "started" in r["output"]

    async def test_bash_keeps_head_and_tail(self):
        r = await exec_bash({"command": f'{PY} -c "print(\'HEAD\' + \'x\' * 300000 + \'TAIL\')"'})
        out = r["output"]
        assert out.startswith("HEAD")
        assert out.rstrip().endswith("TAIL")
        assert "elided" in out
        assert len(out) < 60_000

    def test_head_tail_passthrough_when_small(self):
        assert _head_tail("abc", 10, 10) == "abc"
        s = _head_tail("a" * 50 + "b" * 50, 10, 10)
        assert s.startswith("a" * 10) and s.endswith("b" * 10) and "elided" in s

    async def test_search_files_async(self, tmp_path):
        (tmp_path / "f.txt").write_text("needle here\n", encoding="utf-8")
        r = await exec_search_files({"pattern": "needle", "path": str(tmp_path)})
        assert "needle" in r.get("matches", "") or "error" in r

    async def test_file_tools_still_work(self, tmp_path):
        p = tmp_path / "a.txt"
        assert (await shell_tools.exec_write_file({"path": str(p), "content": "hi\n"}))["status"] == "ok"
        assert "hi" in (await shell_tools.exec_read_file({"path": str(p)}))["content"]
        r = await shell_tools.exec_edit_file({"path": str(p), "old_string": "hi", "new_string": "yo"})
        assert r["status"] == "ok"
        names = [e["name"] for e in (await shell_tools.exec_list_dir({"path": str(tmp_path)}))["entries"]]
        assert names == ["a.txt"]


# ---------------------------------------------------------------------------
# Scripted provider (same shape as test_loop_hardening.ScriptedProvider)
# ---------------------------------------------------------------------------

class ScriptedProvider(Provider):
    def __init__(self, script: list[Any], summary_responses: list[Any] | None = None,
                 model: str = "test-model") -> None:
        self._script = list(script)
        self._summary = list(summary_responses or [])
        self.stream_calls = 0
        self.summary_calls = 0
        self.seen_messages: list[list[dict]] = []
        self.summary_requests: list[dict] = []
        self._active_model = model

    @property
    def name(self) -> str:
        return "scripted"

    async def send(self, *, messages, system="", model="", max_tokens=1024,
                   tools=None, temperature=0.0) -> ProviderResponse:
        self.summary_calls += 1
        self.summary_requests.append({
            "messages": [dict(m) for m in messages], "system": system,
            "tools": tools,
        })
        item = self._summary.pop(0) if self._summary else ProviderResponse(
            text="SUMMARY", stop_reason="end_turn")
        if isinstance(item, Exception):
            raise item
        return item

    async def send_stream(self, *, messages, system="", model="", max_tokens=1024,
                          tools=None, temperature=0.0, on_chunk=None,
                          on_thinking=None) -> ProviderResponse:
        self.stream_calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(**kw) -> ProviderResponse:
    kw.setdefault("usage", Usage(input_tokens=5, output_tokens=2))
    return ProviderResponse(**kw)


def _tool_results(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            out.extend(b for b in c if isinstance(b, dict) and b.get("type") == "tool_result")
    return out


# ---------------------------------------------------------------------------
# §18 Plain-text tool results
# ---------------------------------------------------------------------------

class TestPlainTextResults:
    def test_string_passthrough(self):
        assert format_tool_result("hello\nworld") == "hello\nworld"

    def test_shell_shape(self):
        txt = format_tool_result({"output": "line1\nline2", "exit_code": 2})
        assert txt == "Exit code: 2\nOutput:\nline1\nline2"

    def test_shell_timeout_shape(self):
        txt = format_tool_result({"error": "timed out", "output": "partial"})
        assert "Error: timed out" in txt and txt.endswith("partial")

    def test_single_text_field(self):
        assert format_tool_result({"content": "1\tfoo"}) == "1\tfoo"
        assert format_tool_result({"matches": "a.py:1:x"}) == "a.py:1:x"
        assert format_tool_result({"error": "nope"}) == "Error: nope"

    def test_structured_stays_json(self):
        import json
        r = {"entries": [{"name": "a", "type": "file", "size": 1}]}
        assert json.loads(format_tool_result(r)) == r
        r2 = {"status": "ok", "path": "/x", "bytes": 3}
        assert json.loads(format_tool_result(r2)) == r2

    async def test_loop_sends_plain_text(self):
        p = ScriptedProvider([
            _resp(tool_calls=[ToolCall(id="c1", name="bash", input={"command": "x"})],
                  stop_reason="tool_use"),
            _resp(text="done", stop_reason="end_turn"),
        ])

        async def ex(name, inp):
            return {"output": "hi there", "exit_code": 0}

        await p.run_agent_loop(message="go", tools=[{"name": "bash"}],
                               tool_executor=ex)
        (tr,) = _tool_results(p.seen_messages[1])
        assert tr["content"] == "Exit code: 0\nOutput:\nhi there"

    def test_adapters_accept_text_results(self):
        from atn.providers.openai_compat import _to_openai_messages
        from atn.providers.ollama import _translate_history
        hist = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "bash", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1",
                 "content": format_tool_result({"output": "ok", "exit_code": 0})}]},
        ]
        oai = _to_openai_messages(hist, "")
        tool_msgs = [m for m in oai if m.get("role") == "tool"]
        assert tool_msgs and tool_msgs[0]["content"].startswith("Exit code: 0")
        oll = _translate_history(hist)
        tool_msgs = [m for m in oll if m.get("role") == "tool"]
        assert tool_msgs and tool_msgs[0]["content"].startswith("Exit code: 0")


# ---------------------------------------------------------------------------
# §19 Context fullness from provider-reported usage
# ---------------------------------------------------------------------------

from atn.providers.base import _next_input_estimate  # noqa: E402


class TestUsageBasedEstimate:
    def test_anchor_plus_appended(self):
        msgs = [{"role": "user", "content": "x" * 400},
                {"role": "assistant", "content": "y" * 40}]
        # Reported 1000 tokens for a 1-message request; 40 chars appended since.
        assert _next_input_estimate("", msgs, (1000, 1)) == 1000 + 10

    def test_fallback_without_anchor(self):
        msgs = [{"role": "user", "content": "x" * 400}]
        assert _next_input_estimate("s" * 40, msgs, None) == 110

    def test_stale_anchor_falls_back(self):
        msgs = [{"role": "user", "content": "x" * 400}]
        # Anchor longer than the stack (rewritten) -> chars/4.
        assert _next_input_estimate("", msgs, (5000, 3)) == 100

    async def test_ceiling_uses_reported_usage(self):
        # Tiny stack, but the provider says the request was 50k tokens (tool
        # schemas, system prompt...). chars/4 alone would never trip 10k.
        p = ScriptedProvider([
            _resp(tool_calls=[ToolCall(id="c1", name="t", input={"a": 1})],
                  stop_reason="tool_use",
                  usage=Usage(input_tokens=2_000, cache_read_tokens=48_000)),
            _resp(text="never", stop_reason="end_turn"),
        ])

        async def ex(name, inp):
            return "ok"

        r = await p.run_agent_loop(message="go", tools=[{"name": "t"}],
                                   tool_executor=ex, per_turn_input_max=10_000)
        assert r.stop_reason == "per_turn_input_exceeded"
        assert p.stream_calls == 1

    async def test_zero_usage_uses_char_estimate(self):
        p = ScriptedProvider([
            _resp(tool_calls=[ToolCall(id="c1", name="t", input={"a": 1})],
                  stop_reason="tool_use", usage=Usage()),
            _resp(text="done", stop_reason="end_turn", usage=Usage()),
        ])

        async def ex(name, inp):
            return "ok"

        r = await p.run_agent_loop(message="go", tools=[{"name": "t"}],
                                   tool_executor=ex, per_turn_input_max=10_000)
        assert r.stop_reason == "end_turn"
