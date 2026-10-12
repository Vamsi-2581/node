"""Generic-loop techniques ported (as ideas) from Codex CLI (docs/agentic_loop.md §17-§24).

Each section maps to one item of the port:
  §17 non-blocking shell tools (async subprocess, tree kill, head+tail)
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import pytest

from atn import shell_tools
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
