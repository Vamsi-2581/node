"""AgentProcessRegistry: the daemon-side view of an agent's background tasks
(shells, subagents, monitors) mirrored from the Claude-Max bridge."""
from __future__ import annotations

import pytest

from atn.events import Event, EventBus, EventType
from atn.runtime.agent_processes import AgentProcessRegistry


def _task(tid, status="running", **kw):
    return {"task_id": tid, "task_type": "local_bash", "description": f"cmd {tid}",
            "status": status, "started_at": 1000, **kw}


async def _emit(bus, agent_id, tasks):
    await bus.emit(Event(type=EventType.AGENT_PROCESSES, source=agent_id,
                         data={"agent_id": agent_id, "tasks": tasks}))


@pytest.mark.asyncio
async def test_event_updates_list_per_agent():
    bus = EventBus()
    reg = AgentProcessRegistry(bus, stop_fn=None)
    await _emit(bus, "kevin", [_task("a"), _task("b", "completed", ended_at=2000)])
    await _emit(bus, "other", [_task("c")])
    procs = reg.list()
    assert [t["task_id"] for t in procs["kevin"]] == ["a", "b"]
    assert procs["kevin"][0]["kind"] == "shell"
    assert reg.running_count("kevin") == 1
    assert list(reg.list("other")) == ["other"]
    # A later event updates the task in place.
    await _emit(bus, "kevin", [_task("a", "stopped", ended_at=3000)])
    assert reg.running_count("kevin") == 0


@pytest.mark.asyncio
async def test_output_reads_only_reported_files(tmp_path):
    bus = EventBus()
    reg = AgentProcessRegistry(bus, stop_fn=None)
    out = tmp_path / "a.output"
    out.write_text("x" * 100 + "tail", encoding="utf-8")
    await _emit(bus, "kevin", [_task("a", output_file=str(out))])
    r = reg.output("kevin", "a", max_bytes=10)
    assert r["text"] == "xxxxxxtail" and r["truncated"] and r["size"] == 104
    assert reg.output("kevin", "nope")["error"] == "unknown task"
    # Another agent cannot read kevin's task.
    assert reg.output("other", "a")["error"] == "unknown task"
    # The public listing never exposes the path.
    assert "output_file" not in reg.list("kevin")["kevin"][0]


@pytest.mark.asyncio
async def test_stop_routes_running_tasks_only():
    bus = EventBus()
    calls = []

    async def stop_fn(agent_id, task_id):
        calls.append((agent_id, task_id))
        return True

    reg = AgentProcessRegistry(bus, stop_fn=stop_fn)
    await _emit(bus, "kevin", [_task("a"), _task("b", "completed")])
    assert (await reg.stop("kevin", "a"))["status"] == "stopping"
    assert calls == [("kevin", "a")]
    assert "error" in await reg.stop("kevin", "b")
    assert "error" in await reg.stop("kevin", "zzz")


@pytest.mark.asyncio
async def test_finished_tasks_are_capped():
    bus = EventBus()
    reg = AgentProcessRegistry(bus, stop_fn=None)
    await _emit(bus, "kevin", [_task(f"t{i}", "completed", ended_at=i) for i in range(30)]
                + [_task("live")])
    ids = [t["task_id"] for t in reg.list("kevin")["kevin"]]
    assert "live" in ids and len(ids) == 21
    assert "t0" not in ids and "t29" in ids
