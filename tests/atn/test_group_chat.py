"""Group threads (atn/group_chat.py, docs/group_chat.md) and sender
attribution for post_message / delegate_message / inbox drain."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from atn.agent_tools import execute_tool
from atn.events import EventBus, EventType
from atn.group_chat import ADMIN_SENDER, DELIVERY_MAX_TURNS, WAKE_KEY, GroupChatService
from atn.models import AgentDefinition, InboxMessage, MessagePriority, MessageType
from atn.providers.base import ProviderResponse, Usage


def _make_runtime(bus: EventBus, tmp_path: Path):
    from atn.config import ATNConfig
    from atn.runtime import Runtime

    data_dir = tmp_path / "data"
    agents_dir = tmp_path / "agents"
    data_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    config = ATNConfig(data_dir=data_dir, agents_dir=agents_dir)
    config.autonet.enabled = False
    config.voice.enabled = False
    return Runtime(bus, data_dir=data_dir, config=config)


async def _fleet(tmp_path, *, events=None):
    bus = EventBus()
    if events is not None:
        async def cap(e):
            events.append(e)
        bus.subscribe(None, cap)
    rt = _make_runtime(bus, tmp_path)
    await rt.register_agent(AgentDefinition(id="parent", name="Parent"))
    await rt.register_agent(AgentDefinition(id="a", name="A", parent_id="parent"))
    await rt.register_agent(AgentDefinition(id="b", name="B", parent_id="parent"))
    await rt.register_agent(AgentDefinition(id="stranger", name="S"))
    return rt


def _wakes(rt, agent_id, gid):
    return [m for m in rt.inbox.peek(agent_id) if (m.data or {}).get(WAKE_KEY) == gid]


async def _owner(rt, tool, **args):
    return await execute_tool(tool, args, rt, caller_id="")


async def _as(rt, agent, tool, **args):
    return await execute_tool(tool, args, rt, caller_id=agent)


# ---------------------------------------------------------------------------
# Posting, waking, coalescing, rendering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_post_wakes_all_members_coalesced(tmp_path):
    events: list = []
    rt = await _fleet(tmp_path, events=events)
    g = (await _owner(rt, "group_create", name="crew", members=["a", "b"]))["group"]
    gid = g["id"]
    assert g["seats"] == {"a": "agent", "b": "agent"}

    r1 = await _owner(rt, "group_post", group_id=gid, content="hello team")
    assert r1["turn"]["sender_id"] == ADMIN_SENDER
    assert sorted(r1["woken"]) == ["a", "b"]
    await _owner(rt, "group_post", group_id=gid, content="second")
    # Two posts, still one queued wake per member.
    assert len(_wakes(rt, "a", gid)) == 1
    assert len(_wakes(rt, "b", gid)) == 1

    text = rt.groups.render_delivery("a", gid)
    assert "[group crew] admin: hello team" in text
    assert "[group crew] admin: second" in text
    assert "group_post" in text and "stay silent" in text
    assert rt.groups.render_delivery("a", gid) == ""  # cursor advanced

    kinds = [e.type for e in events]
    assert EventType.GROUP_TURN in kinds and EventType.GROUP_UPDATED in kinds


@pytest.mark.asyncio
async def test_agent_post_wakes_others_not_self_and_cannot_forge(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    r = await _as(rt, "a", "group_post", group_id=gid, content="from a",
                  sender_id="b", source="admin", _caller_id="b")
    assert r["turn"]["sender_id"] == "a"
    assert r["woken"] == ["b"]
    assert _wakes(rt, "a", gid) == []
    assert "[group g] a: from a" in rt.groups.render_delivery("b", gid)
    # a's own post is never delivered back to it.
    assert rt.groups.render_delivery("a", gid) == ""


@pytest.mark.asyncio
async def test_non_member_refused(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    r = await _as(rt, "stranger", "group_post", group_id=gid, content="hi")
    assert "not a member" in r["error"]
    r = await _as(rt, "stranger", "group_read", group_id=gid)
    assert "not a member" in r["error"]
    assert (await _as(rt, "stranger", "group_list"))["groups"] == []
    assert len((await _as(rt, "a", "group_list"))["groups"]) == 1


@pytest.mark.asyncio
async def test_group_pass_posts_nothing(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    r = await _as(rt, "a", "group_pass", group_id=gid)
    assert r["status"] == "passed"
    assert rt.groups.turns(gid) == []
    assert _wakes(rt, "b", gid) == []


@pytest.mark.asyncio
async def test_far_behind_delivery_is_capped(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    for i in range(DELIVERY_MAX_TURNS + 5):
        await _owner(rt, "group_post", group_id=gid, content=f"m{i}")
    text = rt.groups.render_delivery("a", gid)
    assert "5 earlier unseen turn(s) not shown" in text
    assert "admin: m0\n" not in text
    assert f"admin: m{DELIVERY_MAX_TURNS + 4}" in text


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_take_seat_mutes_agent_and_release_redelivers(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    await _owner(rt, "group_post", group_id=gid, content="before")
    assert len(_wakes(rt, "b", gid)) == 1

    g = (await _owner(rt, "group_take_seat", group_id=gid, agent_id="b"))["group"]
    assert g["seats"]["b"] == "human"
    assert _wakes(rt, "b", gid) == []  # pending wake dropped

    r = await _owner(rt, "group_post", group_id=gid, content="while held")
    assert r["woken"] == ["a"]
    assert _wakes(rt, "b", gid) == []
    r = await _as(rt, "b", "group_post", group_id=gid, content="me?")
    assert "held by the owner" in r["error"]
    assert rt.groups.render_delivery("b", gid) == ""

    rel = await _owner(rt, "group_release_seat", group_id=gid, agent_id="b")
    assert rel["redelivered"] is True
    assert rel["group"]["seats"]["b"] == "agent"
    text = rt.groups.render_delivery("b", gid)
    assert "admin: before" in text and "admin: while held" in text


@pytest.mark.asyncio
async def test_owner_ops_not_agent_callable(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    for op in ("group_take_seat", "group_release_seat", "group_add_member",
               "group_remove_member", "group_delete", "group_get"):
        r = await _as(rt, "a", op, group_id=gid, agent_id="b")
        assert "not available" in r["error"], op


def test_owner_ops_are_owner_only_on_ws():
    from atn.ws_server import OWNER_ONLY_TOOLS
    assert {"group_take_seat", "group_release_seat", "group_delete",
            "group_add_member", "group_remove_member", "group_get"} <= OWNER_ONLY_TOOLS


@pytest.mark.asyncio
async def test_member_add_remove_delete(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a"]))["group"]["id"]
    await _owner(rt, "group_post", group_id=gid, content="context")
    g = (await _owner(rt, "group_add_member", group_id=gid, agent_id="b"))["group"]
    assert g["members"] == ["a", "b"]
    # A late joiner's first delivery carries the backlog.
    await _owner(rt, "group_post", group_id=gid, content="welcome b")
    assert "admin: context" in rt.groups.render_delivery("b", gid)
    await _owner(rt, "group_post", group_id=gid, content="x")
    g = (await _owner(rt, "group_remove_member", group_id=gid, agent_id="b"))["group"]
    assert g["members"] == ["a"] and _wakes(rt, "b", gid) == []
    assert (await _owner(rt, "group_delete", group_id=gid))["deleted"] is True
    assert (await _owner(rt, "group_list"))["groups"] == []
    assert _wakes(rt, "a", gid) == []


# ---------------------------------------------------------------------------
# Parent-created groups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parent_creates_group_over_children_only(tmp_path):
    rt = await _fleet(tmp_path)
    r = await _as(rt, "parent", "group_create", name="kids", members=["a", "stranger"])
    assert "direct children" in r["error"]
    r = await _as(rt, "a", "group_create", name="x", members=["b"])
    assert "direct children" in r["error"]  # siblings are not children
    g = (await _as(rt, "parent", "group_create", name="kids", members=["a", "b"]))["group"]
    assert g["members"] == ["parent", "a", "b"]
    assert g["created_by"] == "parent"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_groups_persist_with_cursors(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    await _owner(rt, "group_post", group_id=gid, content="one")
    rt.groups.render_delivery("a", gid)
    await _owner(rt, "group_post", group_id=gid, content="two")
    await _owner(rt, "group_take_seat", group_id=gid, agent_id="b")

    again = GroupChatService(tmp_path / "data" / "groups", None)
    s = again.summary(gid)
    assert s["turn_count"] == 2 and s["seats"]["b"] == "human"
    assert [t["content"] for t in again.turns(gid)] == ["one", "two"]
    assert again.get(gid)["cursors"]["a"] == 1


# ---------------------------------------------------------------------------
# Running members: live injection, coalesced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_running_member_gets_live_injection(tmp_path):
    rt = await _fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="g", members=["a", "b"]))["group"]["id"]
    rt.registry._running_count["b"] = 1
    sent: list[tuple[str, str]] = []
    gate = asyncio.Event()

    async def fake_send(agent_id, text):
        sent.append((agent_id, text))
        await gate.wait()
        return "injected"

    with patch.object(rt.control, "send_delegate_message", side_effect=fake_send):
        await _as(rt, "a", "group_post", group_id=gid, content="p1")
        await asyncio.sleep(0)
        await _as(rt, "a", "group_post", group_id=gid, content="p2")
        await _as(rt, "a", "group_post", group_id=gid, content="p3")
        gate.set()
        await rt.groups.wait_idle()

    assert _wakes(rt, "b", gid) == []
    # p1 alone, then p2+p3 coalesced into one follow-up injection.
    assert len(sent) == 2
    assert "a: p1" in sent[0][1]
    assert "a: p2" in sent[1][1] and "a: p3" in sent[1][1]


# ---------------------------------------------------------------------------
# Engine drain: group wake rendering, sender labels, member tool surface
# ---------------------------------------------------------------------------

def _mock_provider(captured: list):
    resp = ProviderResponse(text="ok", usage=Usage(input_tokens=1, output_tokens=1),
                            stop_reason="end_turn", model="sonnet")

    async def loop(**kwargs):
        captured.append(kwargs)
        return resp

    p = AsyncMock()
    p.run_agent_loop = loop
    p.close = AsyncMock()
    p.interrupt = AsyncMock()
    return p


async def _run_once(rt, agent_id):
    done = asyncio.Event()

    async def on_done(e):
        if (e.data or {}).get("agent_id") == agent_id:
            done.set()
    rt.events.subscribe(EventType.EXECUTION_COMPLETED, on_done)
    rt.events.subscribe(EventType.EXECUTION_FAILED, on_done)
    await rt.activate_agent(agent_id)
    await rt.trigger_run(agent_id, source="test")
    await asyncio.wait_for(done.wait(), 10)


async def _cog_fleet(tmp_path):
    from atn.models import AgentMode
    bus = EventBus()
    rt = _make_runtime(bus, tmp_path)
    for aid, parent in (("parent", None), ("a", "parent"), ("b", "parent")):
        await rt.register_agent(AgentDefinition(
            id=aid, name=aid.upper(), parent_id=parent, mode=AgentMode.COGNITIVE,
            provider="claude_max", cognitive_model="sonnet",
            tools=["planning"]))
    return rt


@pytest.mark.asyncio
async def test_engine_renders_group_wake_and_surfaces_group_tools(tmp_path):
    rt = await _cog_fleet(tmp_path)
    gid = (await _owner(rt, "group_create", name="crew", members=["a", "b"]))["group"]["id"]
    await _as(rt, "a", "group_post", group_id=gid, content="anyone?")
    await _owner(rt, "group_post", group_id=gid, content="me too")
    captured: list = []
    with patch("atn.runtime.provider_manager.BridgeProvider",
               return_value=_mock_provider(captured)):
        rt.registry._status["b"] = rt.registry._status.get("b")
        await _run_once(rt, "b")
    turns = rt.get_agent_conversation_store("b").get_turns()
    user = [t.content for t in turns if t.role == "user"][-1]
    assert "[group crew] a: anyone?" in user
    assert "[group crew] admin: me too" in user
    tool_names = {t["name"] for t in captured[-1].get("tools") or []}
    assert {"group_post", "group_read", "group_pass"} <= tool_names
    assert rt.groups.render_delivery("b", gid) == ""


@pytest.mark.asyncio
async def test_engine_labels_agent_sender_not_owner(tmp_path):
    rt = await _cog_fleet(tmp_path)
    rt.inbox.post(InboxMessage(id="1", source="parent", target="a",
                               type=MessageType.WORK, priority=MessagePriority.HIGH,
                               data={"instruction": "do the thing"}))
    rt.inbox.post(InboxMessage(id="2", source="user", target="a",
                               type=MessageType.WORK, priority=MessagePriority.HIGH,
                               data={"instruction": "owner says hi"}))
    with patch("atn.runtime.provider_manager.BridgeProvider",
               return_value=_mock_provider([])):
        await _run_once(rt, "a")
    user = [t.content for t in rt.get_agent_conversation_store("a").get_turns()
            if t.role == "user"][-1]
    assert "[from parent] do the thing" in user
    assert "owner says hi" in user and "[from user]" not in user


# ---------------------------------------------------------------------------
# post_message / delegate_message attribution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_message_stamps_agent_caller_and_ignores_forged_source(tmp_path):
    rt = await _fleet(tmp_path)
    r = await _as(rt, "a", "post_message", target="b", message_type="info",
                  source="owner", data={"instruction": "hi sibling"})
    assert "error" not in r
    msgs = rt.inbox.peek("b")
    assert msgs[-1].source == "a"


@pytest.mark.asyncio
async def test_post_message_owner_keeps_owner_source(tmp_path):
    rt = await _fleet(tmp_path)
    await _owner(rt, "post_message", target="b", message_type="info",
                 source="user", data={"instruction": "from the UI"})
    assert rt.inbox.peek("b")[-1].source == "user"
    await _owner(rt, "post_message", target="b", message_type="info")
    assert rt.inbox.peek("b")[-1].source == "owner"


@pytest.mark.asyncio
async def test_delegate_message_attribution(tmp_path):
    rt = await _fleet(tmp_path)
    sent = []

    async def not_running(agent_id, text):
        sent.append(text)
        return False

    with patch.object(rt.control, "send_delegate_message", side_effect=not_running):
        r = await _as(rt, "parent", "delegate_message", agent_id="a", content="steer")
        assert r["status"] == "queued"
        assert sent[-1] == "[from parent] steer"
        assert rt.inbox.peek("a")[-1].source == "parent"
        assert rt.inbox.peek("a")[-1].data["instruction"] == "steer"

        await _owner(rt, "delegate_message", agent_id="a", content="owner steer")
        assert sent[-1] == "owner steer"
        assert rt.inbox.peek("a")[-1].source == "owner"
