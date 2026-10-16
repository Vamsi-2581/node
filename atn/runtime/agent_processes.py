"""Background processes an agent left running (shells, subagents, monitors).

The Claude-Max bridge mirrors the Agent SDK's background-task stream as
``EventType.AGENT_PROCESSES`` events carrying the agent's full task list. This
registry keeps the latest list per agent so the frontend can draw the tasks as
leaves under their agent, read a task's output and stop it.

The bridge keeps an agent's session open while any task runs (Decision
2026-10-16, option A), so a "running" task here always has a live session to
stop it through.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..events import Event, EventBus, EventType

log = logging.getLogger(__name__)

# Finished tasks kept per agent for the UI (newest last).
_KEEP_FINISHED = 20
_MAX_OUTPUT_BYTES = 256 * 1024


class AgentProcessRegistry:
    def __init__(self, events: EventBus,
                 stop_fn: Callable[[str, str], Awaitable[bool]]) -> None:
        self._tasks: dict[str, dict[str, dict[str, Any]]] = {}
        self._stop_fn = stop_fn
        events.subscribe(EventType.AGENT_PROCESSES, self._on_event)

    async def _on_event(self, event: Event) -> None:
        data = event.data or {}
        agent_id = data.get("agent_id") or event.source
        tasks = data.get("tasks")
        if not agent_id or not isinstance(tasks, list):
            return
        book = self._tasks.setdefault(agent_id, {})
        for t in tasks:
            if isinstance(t, dict) and t.get("task_id"):
                book[str(t["task_id"])] = {**book.get(str(t["task_id"]), {}), **t}
        finished = sorted((t for t in book.values() if t.get("status") != "running"),
                          key=lambda t: t.get("ended_at") or 0)
        for t in finished[:-_KEEP_FINISHED]:
            book.pop(t["task_id"], None)

    def list(self, agent_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        ids = [agent_id] if agent_id else list(self._tasks)
        out: dict[str, list[dict[str, Any]]] = {}
        for aid in ids:
            tasks = sorted(self._tasks.get(aid, {}).values(),
                           key=lambda t: t.get("started_at") or 0)
            if tasks:
                out[aid] = [_public(t) for t in tasks]
        return out

    def running_count(self, agent_id: str) -> int:
        return sum(1 for t in self._tasks.get(agent_id, {}).values()
                   if t.get("status") == "running")

    def output(self, agent_id: str, task_id: str,
               max_bytes: int = 64 * 1024) -> dict[str, Any]:
        """Tail of a task's output file. Only files the bridge reported for
        this agent's task are read."""
        t = self._tasks.get(agent_id, {}).get(task_id)
        if t is None:
            return {"error": "unknown task"}
        path = t.get("output_file") or ""
        if not path:
            return {"text": "", "size": 0, "truncated": False,
                    "note": "no output file reported for this task"}
        max_bytes = max(1, min(int(max_bytes or 0) or 64 * 1024, _MAX_OUTPUT_BYTES))
        try:
            p = Path(path)
            size = p.stat().st_size
            with p.open("rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read(max_bytes)
        except FileNotFoundError:
            return {"text": "", "size": 0, "truncated": False,
                    "note": "output file not found"}
        except OSError as exc:
            return {"error": f"cannot read output: {exc}"}
        return {"text": data.decode("utf-8", errors="replace"), "size": size,
                "truncated": size > max_bytes}

    async def stop(self, agent_id: str, task_id: str) -> dict[str, Any]:
        t = self._tasks.get(agent_id, {}).get(task_id)
        if t is None:
            return {"error": "unknown task"}
        if t.get("status") != "running":
            return {"error": f"task is {t.get('status')}"}
        if not await self._stop_fn(agent_id, task_id):
            return {"error": "agent has no live session to stop the task through"}
        return {"status": "stopping", "agent_id": agent_id, "task_id": task_id}


def _public(t: dict[str, Any]) -> dict[str, Any]:
    started = t.get("started_at") or 0
    ended = t.get("ended_at") or 0
    now_ms = time.time() * 1000
    return {
        "task_id": t.get("task_id"),
        "task_type": t.get("task_type") or "",
        "kind": _kind(t.get("task_type") or ""),
        "description": t.get("description") or "",
        "status": t.get("status") or "running",
        "started_at": started,
        "ended_at": ended or None,
        "runtime_ms": int((ended or now_ms) - started) if started else None,
        "has_output": bool(t.get("output_file")),
    }


def _kind(task_type: str) -> str:
    """Friendly label for the UI: shell, subagent, monitor, workflow."""
    tt = task_type.lower()
    if "bash" in tt or "shell" in tt:
        return "shell"
    if "agent" in tt:
        return "subagent"
    if "monitor" in tt or "mcp" in tt:
        return "monitor"
    if "workflow" in tt:
        return "workflow"
    return tt or "task"
