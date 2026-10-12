"""Generic-loop techniques ported (as ideas) from Codex CLI (docs/agentic_loop.md §17-§24).

Each section maps to one item of the port:
  §17 non-blocking shell tools (async subprocess, tree kill, head+tail)
  §18 plain-text tool results
  §19 context fullness from provider-reported usage
  §20 cache-friendly compaction
  §21 parallel read-only tools
  §22 update_plan
  §23 context headroom signal
  §24 history hygiene (whole-stack repair, max_tokens tool call, backoff)
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


# ---------------------------------------------------------------------------
# §20 Cache-friendly compaction
# ---------------------------------------------------------------------------

from atn.providers.base import (  # noqa: E402
    COMPACTION_INLINE_PROMPT,
    ContextOverflowError,
    ToolDefinition,
    _drop_oldest_history,
)


def _long_history(n_pairs: int = 8, size: int = 2000) -> list[dict]:
    msgs: list[dict] = [{"role": "user", "content": "ORIGINAL TASK"}]
    for i in range(n_pairs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"u{i}", "name": "t", "input": {"i": i}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"u{i}", "content": "r" * size}]})
    return msgs


class TestCacheFriendlyCompaction:
    async def test_same_system_tools_and_prefix(self):
        p = ScriptedProvider([])
        tools = [ToolDefinition(name="t", description="d", input_schema={})]
        p._run_tool_defs = tools
        msgs = _long_history()
        out = await p._compact_messages(list(msgs), system="SYS", model="m")
        (req,) = p.summary_requests
        assert req["system"] == "SYS"
        assert req["tools"] is tools
        # Whole history is the prefix, instruction appended last.
        assert req["messages"][:-1] == msgs
        assert req["messages"][-1] == {"role": "user", "content": COMPACTION_INLINE_PROMPT}
        assert "SUMMARY" in out[0]["content"]

    async def test_overflow_drops_oldest_and_retries(self):
        p = ScriptedProvider([], summary_responses=[
            ContextOverflowError("prompt is too long"),
            ContextOverflowError("prompt is too long"),
            ProviderResponse(text="S2", stop_reason="end_turn"),
        ])
        msgs = _long_history()
        out = await p._compact_messages(list(msgs), system="SYS", model="m")
        assert p.summary_calls == 3
        sizes = [len(r["messages"]) for r in p.summary_requests]
        assert sizes[0] > sizes[1] > sizes[2]
        # Each retried request starts on a plain user message (no orphan results).
        for r in p.summary_requests[1:]:
            first = r["messages"][0]
            assert first["role"] == "user"
            assert not (isinstance(first["content"], list) and any(
                b.get("type") == "tool_result" for b in first["content"]))
        assert "S2" in out[0]["content"]

    async def test_overflow_exhausted_falls_back_to_hard_truncate(self):
        p = ScriptedProvider([], summary_responses=[
            ContextOverflowError("prompt is too long")] * 10)
        msgs = _long_history()
        out = await p._compact_messages(list(msgs), system="SYS", model="m")
        assert "Context truncated" in out[0]["content"]

    async def test_tool_call_instead_of_text_retries_without_tools(self):
        p = ScriptedProvider([], summary_responses=[
            ProviderResponse(text="", tool_calls=[ToolCall(id="x", name="t", input={})],
                             stop_reason="tool_use"),
            ProviderResponse(text="S", stop_reason="end_turn"),
        ])
        p._run_tool_defs = [ToolDefinition(name="t", description="d", input_schema={})]
        out = await p._compact_messages(_long_history(), system="SYS", model="m")
        assert p.summary_requests[1]["tools"] is None
        assert out[0]["content"].count("S") >= 1

    def test_drop_oldest_keeps_valid_start(self):
        msgs = _long_history(n_pairs=10)
        rest = _drop_oldest_history(msgs)
        assert len(rest) < len(msgs)
        assert rest == [] or rest[0]["role"] == "user"


# ---------------------------------------------------------------------------
# §21 Parallel read-only tools
# ---------------------------------------------------------------------------

from atn.providers.base import (  # noqa: E402
    PARALLEL_SAFE_TOOLS,
    parallel_safe_tool_names,
)


class _Recorder:
    """Tool executor that records start/end order and peak concurrency."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.log: list[str] = []

    async def __call__(self, name, inp):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.log.append(f"start:{inp['k']}")
        # Later calls finish first, to prove results keep call order.
        await asyncio.sleep(self.delay * (10 - int(inp["k"][-1])) / 10)
        self.log.append(f"end:{inp['k']}")
        self.active -= 1
        return f"result-{inp['k']}"


def _calls(*specs):
    return [ToolCall(id=f"c{i}", name=n, input={"k": f"{n}{i}"})
            for i, n in enumerate(specs)]


class TestParallelTools:
    def test_allowlist_and_flags(self):
        names = parallel_safe_tool_names([
            {"name": "my_reader", "parallel_safe": True},
            {"name": "read_file", "parallel_safe": False},
        ])
        assert "my_reader" in names
        assert "read_file" not in names
        assert "search_files" in names
        assert "bash" not in PARALLEL_SAFE_TOOLS
        assert "write_file" not in PARALLEL_SAFE_TOOLS

    async def _run(self, calls, rec):
        p = ScriptedProvider([
            _resp(tool_calls=calls, stop_reason="tool_use"),
            _resp(text="done", stop_reason="end_turn"),
        ])
        await p.run_agent_loop(message="go", tools=[{"name": c.name} for c in calls],
                               tool_executor=rec)
        return _tool_results(p.seen_messages[1])

    async def test_reads_run_concurrently_in_order(self):
        rec = _Recorder()
        calls = _calls("read_file", "search_files", "list_directory")
        trs = await self._run(calls, rec)
        assert rec.peak == 3
        assert [t["tool_use_id"] for t in trs] == ["c0", "c1", "c2"]
        assert [t["content"] for t in trs] == [
            "result-read_file0", "result-search_files1", "result-list_directory2"]

    async def test_writer_is_exclusive_and_ordered(self):
        rec = _Recorder()
        calls = _calls("read_file", "read_file", "write_file", "read_file")
        trs = await self._run(calls, rec)
        # write_file starts only after both earlier reads ended, and the read
        # after it starts only after it ended.
        w_start = rec.log.index("start:write_file2")
        assert rec.log.index("end:read_file0") < w_start
        assert rec.log.index("end:read_file1") < w_start
        assert rec.log.index("end:write_file2") < rec.log.index("start:read_file3")
        assert [t["tool_use_id"] for t in trs] == ["c0", "c1", "c2", "c3"]

    async def test_non_safe_tools_never_overlap(self):
        rec = _Recorder()
        await self._run(_calls("bash", "bash", "edit_file"), rec)
        assert rec.peak == 1

    async def test_malformed_args_in_batch_not_executed(self):
        rec = _Recorder()
        calls = _calls("read_file", "read_file")
        calls.insert(1, ToolCall(id="bad", name="read_file", input={"raw": "{oops"}))
        trs = await self._run(calls, rec)
        assert [t["tool_use_id"] for t in trs] == ["c0", "bad", "c1"]
        assert trs[1].get("is_error") is True
        assert len([e for e in rec.log if e.startswith("start:")]) == 2

    async def test_loop_guard_still_fires_on_parallel_reads(self):
        same = [ToolCall(id=f"r{i}", name="read_file", input={"k": "x0"}) for i in range(3)]
        p = ScriptedProvider([
            _resp(tool_calls=same[:], stop_reason="tool_use"),
            _resp(tool_calls=[ToolCall(id=f"s{i}", name="read_file", input={"k": "x0"})
                              for i in range(3)], stop_reason="tool_use"),
            _resp(text="never", stop_reason="end_turn"),
        ])
        r = await p.run_agent_loop(message="go", tools=[{"name": "read_file"}],
                                   tool_executor=_Recorder(0))
        assert r.stop_reason in ("repeat_call_limit", "loop_detected")


# ---------------------------------------------------------------------------
# §22 update_plan
# ---------------------------------------------------------------------------

from atn.providers.base import UPDATE_PLAN_TOOL, format_plan  # noqa: E402


class _ToolsSpy(ScriptedProvider):
    async def send_stream(self, *, tools=None, **kw):
        self.last_tools = tools
        return await super().send_stream(tools=tools, **kw)


_PLAN = {"explanation": "start", "plan": [
    {"step": "read code", "status": "completed"},
    {"step": "write fix", "status": "in_progress"},
    {"step": "run tests", "status": "pending"},
]}


class TestUpdatePlan:
    async def test_tool_exposed_and_handled_without_executor(self):
        executed = []

        async def ex(name, inp):
            executed.append(name)
            return "ok"

        p = _ToolsSpy([
            _resp(tool_calls=[ToolCall(id="p1", name="update_plan", input=_PLAN)],
                  stop_reason="tool_use"),
            _resp(text="done", stop_reason="end_turn"),
        ])
        await p.run_agent_loop(message="go", tools=[{"name": "bash"}], tool_executor=ex)
        assert "update_plan" in [t.name for t in p.last_tools]
        assert executed == []
        (tr,) = _tool_results(p.seen_messages[1])
        assert tr["content"] == "Plan updated."
        assert [s["status"] for s in p._run_plan] == ["completed", "in_progress", "pending"]

    async def test_not_added_without_tools(self):
        p = _ToolsSpy([_resp(text="hi", stop_reason="end_turn")])
        await p.run_agent_loop(message="go", tools=[], tool_executor=_noop)
        assert not p.last_tools

    async def test_invalid_plan_is_error(self):
        bad = {"plan": [{"step": "a", "status": "in_progress"},
                        {"step": "b", "status": "in_progress"}]}
        p = ScriptedProvider([
            _resp(tool_calls=[ToolCall(id="p1", name="update_plan", input=bad)],
                  stop_reason="tool_use"),
            _resp(text="done", stop_reason="end_turn"),
        ])
        await p.run_agent_loop(message="go", tools=[{"name": "bash"}], tool_executor=_noop)
        (tr,) = _tool_results(p.seen_messages[1])
        assert tr.get("is_error") and "in_progress" in tr["content"]
        assert p._run_plan == []

    async def test_plan_survives_compaction_and_hard_truncate(self):
        p = ScriptedProvider([])
        p._apply_plan_update(_PLAN)
        out = await p._compact_messages(_long_history(), system="S", model="m")
        assert "[Current plan]" in out[0]["content"]
        assert "[in_progress] write fix" in out[0]["content"]
        p2 = ScriptedProvider([], summary_responses=[RuntimeError("down")])
        p2._apply_plan_update(_PLAN)
        out2 = await p2._compact_messages(_long_history(), system="S", model="m")
        assert "Context truncated" in out2[0]["content"]
        assert "[pending] run tests" in out2[0]["content"]

    def test_plan_in_hard_reset_digest(self):
        p = ScriptedProvider([])
        p._apply_plan_update(_PLAN)
        msgs = _long_history()
        assert p._try_hard_reset(msgs, "TASK")
        assert "[Current plan]" in msgs[0]["content"]
        assert "Note: start" in msgs[0]["content"]

    def test_format_plan_empty(self):
        assert format_plan([]) == ""
        assert UPDATE_PLAN_TOOL["input_schema"]["required"] == ["plan"]


async def _noop(name, inp):
    return "ok"


# ---------------------------------------------------------------------------
# §23 Context headroom signal
# ---------------------------------------------------------------------------

from atn.providers.base import (  # noqa: E402
    _headroom_level,
    compaction_trigger_tokens,
    get_context_window,
)


class TestHeadroomSignal:
    def test_levels(self):
        assert _headroom_level(50, 100) == 0
        assert _headroom_level(70, 100) == 1
        assert _headroom_level(85, 100) == 2
        assert _headroom_level(95, 100) == 3

    async def test_note_appended_once_per_level(self):
        trigger = compaction_trigger_tokens("test-model", get_context_window("test-model"))
        at = lambda frac: Usage(input_tokens=int(trigger * frac))  # noqa: E731
        tc = lambda i: [ToolCall(id=f"c{i}", name="bash", input={"i": i})]  # noqa: E731
        p = ScriptedProvider([
            _resp(tool_calls=tc(0), stop_reason="tool_use", usage=at(0.50)),
            _resp(tool_calls=tc(1), stop_reason="tool_use", usage=at(0.72)),
            _resp(tool_calls=tc(2), stop_reason="tool_use", usage=at(0.74)),
            _resp(tool_calls=tc(3), stop_reason="tool_use", usage=at(0.82)),
            _resp(text="done", stop_reason="end_turn"),
        ])
        await p.run_agent_loop(message="go", tools=[{"name": "bash"}], tool_executor=_noop)
        final = p.seen_messages[-1]
        trs = _tool_results(final)
        notes = ["[Context:" in t["content"] for t in trs]
        assert notes == [False, True, False, True]
        # The system prompt is never touched.
        assert all("[Context:" not in str(m.get("content")) for m in final
                   if m.get("role") == "assistant")


# ---------------------------------------------------------------------------
# §24 History hygiene
# ---------------------------------------------------------------------------

from atn.providers.base import (  # noqa: E402
    ProviderError,
    TRUNCATED_TOOL_CALL_PROMPT,
    _repair_history,
    classify_provider_error,
)


def _au(*ids):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": i, "name": "t", "input": {}} for i in ids]}


def _ur(*ids):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": "r"} for i in ids]}


class TestRepairHistory:
    def test_valid_history_untouched(self):
        msgs = [{"role": "user", "content": "go"}, _au("a", "b"), _ur("a", "b"), _au("c")]
        before = [dict(m) for m in msgs]
        assert _repair_history(msgs) == 0
        assert msgs == before

    def test_missing_result_in_middle(self):
        msgs = [{"role": "user", "content": "go"}, _au("a", "b"), _ur("a"),
                {"role": "assistant", "content": "ok"}]
        assert _repair_history(msgs) == 1
        ids = [b["tool_use_id"] for b in msgs[2]["content"]]
        assert ids == ["a", "b"]
        assert msgs[2]["content"][1]["is_error"] is True

    def test_missing_result_message_inserted(self):
        msgs = [{"role": "user", "content": "go"}, _au("a"),
                {"role": "user", "content": "steer"}]
        assert _repair_history(msgs) == 1
        assert msgs[2]["content"][0]["tool_use_id"] == "a"
        assert msgs[3]["content"] == "steer"

    def test_orphan_result_becomes_text(self):
        # Compaction summary followed by a tail that starts on tool results.
        msgs = [{"role": "user", "content": "summary"}, _ur("gone"), _au("x"), _ur("x")]
        assert _repair_history(msgs) == 1
        assert msgs[1]["content"][0]["type"] == "text"
        assert msgs[3]["content"][0]["tool_use_id"] == "x"

    def test_trailing_assistant_left_alone(self):
        msgs = [{"role": "user", "content": "go"}, _au("a")]
        assert _repair_history(msgs) == 0

    async def test_repair_runs_before_send(self):
        p = ScriptedProvider([_resp(text="done", stop_reason="end_turn")])
        msgs = [{"role": "user", "content": "summary"}, _ur("gone"),
                {"role": "user", "content": "go on"}]

        async def _reduce():
            raise AssertionError("no reduction expected")

        await p._send_stream_with_retry(
            messages=msgs, system="", model="", max_tokens=100,
            tool_defs=None, on_chunk=None, reduce_context=_reduce)
        sent = p.seen_messages[0]
        assert sent[1]["content"][0]["type"] == "text"


class TestMaxTokensToolCall:
    async def test_truncated_tool_call_retried_once(self):
        executed = []

        async def ex(name, inp):
            executed.append(inp)
            return "ok"

        p = ScriptedProvider([
            _resp(text="writing", stop_reason="max_tokens",
                  tool_calls=[ToolCall(id="w1", name="write_file", input={"raw": '{"path": "a'})]),
            _resp(tool_calls=[ToolCall(id="w2", name="write_file", input={"path": "a", "content": "x"})],
                  stop_reason="tool_use"),
            _resp(text="done", stop_reason="end_turn"),
        ])
        r = await p.run_agent_loop(message="go", tools=[{"name": "write_file"}], tool_executor=ex)
        assert r.stop_reason == "end_turn" and r.text == "done"
        assert executed == [{"path": "a", "content": "x"}]
        second = p.seen_messages[1]
        assert second[-1]["content"] == TRUNCATED_TOOL_CALL_PROMPT
        # The cut-off tool call never enters history (no orphan tool_use).
        assert not any(isinstance(m["content"], list) and any(
            b.get("id") == "w1" for b in m["content"]) for m in second)

    async def test_second_truncation_finishes(self):
        trunc = lambda i: _resp(stop_reason="max_tokens", tool_calls=[  # noqa: E731
            ToolCall(id=f"w{i}", name="write_file", input={"raw": "{"})])
        p = ScriptedProvider([trunc(1), trunc(2)])
        r = await p.run_agent_loop(message="go", tools=[{"name": "write_file"}],
                                   tool_executor=_noop)
        assert p.stream_calls == 2
        assert r.stop_reason == "max_tokens"


class TestConnectionBackoff:
    def test_connection_errors_are_transient(self):
        assert classify_provider_error(ConnectionResetError("reset")) == "transient"
        wrapped = ProviderError("send failed")
        wrapped.__cause__ = ConnectionRefusedError("refused")
        assert classify_provider_error(wrapped) == "transient"
        # A real HTTP status keeps its own route.
        assert classify_provider_error(ProviderError("x", status_code=401)) == "fatal"

    async def test_separate_schedule(self, monkeypatch):
        import atn.providers.base as base
        sleeps: list[float] = []

        async def _fast_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(base.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(base.random, "uniform", lambda a, b: 0.0)

        class ConnectError(Exception):
            pass

        p = ScriptedProvider(
            [ConnectError("down")] * 4
            + [ProviderError("busy", status_code=503)]
            + [_resp(text="ok", stop_reason="end_turn")]
        )
        r = await p.run_agent_loop(message="go", tools=[])
        assert r.text == "ok"
        assert sleeps == [1.0, 2.0, 4.0, 8.0, 2.0]

    async def test_retry_after_capped(self, monkeypatch):
        import atn.providers.base as base
        sleeps: list[float] = []

        async def _fast_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(base.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(base.random, "uniform", lambda a, b: 0.0)
        e = ProviderError("rate", status_code=429)
        e.retry_after = 99999
        p = ScriptedProvider([e, _resp(text="ok", stop_reason="end_turn")])
        await p.run_agent_loop(message="go", tools=[])
        assert sleeps == [base._RETRY_AFTER_CAP_S]
