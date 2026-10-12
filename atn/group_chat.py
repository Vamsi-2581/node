"""Group threads: a shared transcript several agents (and the owner) post to.

A group thread exists ALONGSIDE each member's private conversation. Inside a
group, agents are peers: any member may post, and every post wakes every
other member whose seat is held by the agent. See docs/group_chat.md.

Disk layout (under the daemon data dir)::

    groups/<group_id>/meta.json     name, members, seats, cursors, created_*
    groups/<group_id>/turns.jsonl   one turn per line: {seq, sender_id, content, ts}

Senders: an agent id, or ``ADMIN_SENDER`` ("admin") for the human owner.

Delivery: each member keeps a cursor (the last seq it has been shown). A
wake renders only the turns after the cursor (minus the member's own posts),
then advances the cursor. An idle member gets ONE coalesced inbox wake per
group (the turns are rendered when the engine drains the inbox, so several
posts arriving before it runs become one delivery). A running member gets the
rendered turns injected through the same steering path delegate_message
uses. A member whose seat the owner has taken is not woken; when the seat is
released it receives everything since its cursor.

There is no cap on turns (budgets are the bound). A woken agent may stay
silent: ending its run without calling group_post posts nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

ADMIN_SENDER = "admin"
SEAT_AGENT = "agent"
SEAT_HUMAN = "human"

# A delivery shows at most this many unseen turns; older ones are counted
# and the agent is pointed at group_read.
DELIVERY_MAX_TURNS = 20
# One post's content ceiling (characters).
MAX_POST_CHARS = 20_000

# Inbox data key that marks a coalesced group wake.
WAKE_KEY = "group_wake"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GroupError(Exception):
    """A refused group operation (message is user-facing)."""


class GroupChatService:
    """Owns group persistence, membership, seats, cursors and delivery."""

    def __init__(self, root_dir: Path, runtime: Any = None) -> None:
        self._dir = Path(root_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._rt = runtime
        self._groups: dict[str, dict[str, Any]] = {}
        self._turns: dict[str, list[dict[str, Any]]] = {}
        # (agent_id, group_id) with a live-injection task in flight, and the
        # ones that got another post while it was in flight.
        self._inflight: set[tuple[str, str]] = set()
        self._dirty: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        for gdir in sorted(self._dir.iterdir()):
            meta = gdir / "meta.json"
            if not gdir.is_dir() or not meta.exists():
                continue
            try:
                g = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                log.warning("unreadable group meta %s", meta, exc_info=True)
                continue
            turns: list[dict[str, Any]] = []
            tpath = gdir / "turns.jsonl"
            if tpath.exists():
                for line in tpath.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        turns.append(json.loads(line))
                    except Exception:
                        log.warning("skipping bad group turn line in %s", tpath)
            g.setdefault("seats", {m: SEAT_AGENT for m in g.get("members", [])})
            g.setdefault("cursors", {m: 0 for m in g.get("members", [])})
            self._groups[g["id"]] = g
            self._turns[g["id"]] = turns

    def _save_meta(self, gid: str) -> None:
        gdir = self._dir / gid
        gdir.mkdir(parents=True, exist_ok=True)
        tmp = gdir / "meta.json.tmp"
        tmp.write_text(json.dumps(self._groups[gid], indent=2), encoding="utf-8")
        tmp.replace(gdir / "meta.json")

    def _append_turn(self, gid: str, turn: dict[str, Any]) -> None:
        gdir = self._dir / gid
        gdir.mkdir(parents=True, exist_ok=True)
        with open(gdir / "turns.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(turn) + "\n")
        self._turns[gid].append(turn)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _require(self, gid: str) -> dict[str, Any]:
        g = self._groups.get(gid or "")
        if g is None:
            raise GroupError(f"Group '{gid}' not found.")
        return g

    def get(self, gid: str) -> dict[str, Any] | None:
        return self._groups.get(gid)

    def last_seq(self, gid: str) -> int:
        turns = self._turns.get(gid) or []
        return int(turns[-1]["seq"]) if turns else 0

    def summary(self, gid: str) -> dict[str, Any]:
        g = self._require(gid)
        turns = self._turns.get(gid) or []
        return {
            "id": g["id"],
            "name": g["name"],
            "members": list(g["members"]),
            "seats": dict(g["seats"]),
            "created_by": g.get("created_by", ADMIN_SENDER),
            "created_at": g.get("created_at", ""),
            "turn_count": len(turns),
            "last_turn": dict(turns[-1]) if turns else None,
        }

    def list_groups(self, member: str | None = None) -> list[dict[str, Any]]:
        out = []
        for gid, g in self._groups.items():
            if member is not None and member not in g["members"]:
                continue
            out.append(self.summary(gid))
        out.sort(key=lambda s: s.get("created_at", ""))
        return out

    def groups_for(self, agent_id: str) -> list[str]:
        return [gid for gid, g in self._groups.items() if agent_id in g["members"]]

    def turns(self, gid: str, *, after: int = 0,
              limit: int | None = None) -> list[dict[str, Any]]:
        self._require(gid)
        rows = [t for t in self._turns[gid] if int(t["seq"]) > after]
        if limit is not None and limit >= 0:
            rows = rows[-limit:] if limit else []
        return [dict(t) for t in rows]

    def is_member(self, gid: str, agent_id: str) -> bool:
        g = self._groups.get(gid)
        return bool(g) and agent_id in g["members"]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def _known_agent(self, agent_id: str) -> bool:
        if self._rt is None:
            return True
        return self._rt.get_agent(agent_id) is not None

    async def create(self, name: str, members: list[str], *,
                     created_by: str = ADMIN_SENDER) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise GroupError("Missing group 'name'.")
        uniq: list[str] = []
        for m in members or []:
            if not isinstance(m, str) or not m or m == ADMIN_SENDER:
                continue
            if m not in uniq:
                uniq.append(m)
        if not uniq:
            raise GroupError("A group needs at least one agent member.")
        unknown = [m for m in uniq if not self._known_agent(m)]
        if unknown:
            raise GroupError(f"Unknown agent(s): {', '.join(unknown)}")
        gid = "grp_" + uuid4().hex[:10]
        self._groups[gid] = {
            "id": gid,
            "name": name,
            "members": uniq,
            "seats": {m: SEAT_AGENT for m in uniq},
            "cursors": {m: 0 for m in uniq},
            "created_by": created_by,
            "created_at": _now(),
        }
        self._turns[gid] = []
        self._save_meta(gid)
        await self._emit_updated(gid)
        return self.summary(gid)

    async def add_member(self, gid: str, agent_id: str) -> dict[str, Any]:
        g = self._require(gid)
        if not agent_id or agent_id == ADMIN_SENDER:
            raise GroupError("Missing 'agent_id'.")
        if not self._known_agent(agent_id):
            raise GroupError(f"Agent '{agent_id}' not found.")
        if agent_id not in g["members"]:
            g["members"].append(agent_id)
            g["seats"][agent_id] = SEAT_AGENT
            # cursor 0: the first delivery carries the recent backlog
            # (capped to DELIVERY_MAX_TURNS) as context.
            g["cursors"][agent_id] = 0
            self._save_meta(gid)
            await self._emit_updated(gid)
        return self.summary(gid)

    async def remove_member(self, gid: str, agent_id: str) -> dict[str, Any]:
        g = self._require(gid)
        if agent_id not in g["members"]:
            raise GroupError(f"'{agent_id}' is not a member of '{gid}'.")
        g["members"].remove(agent_id)
        g["seats"].pop(agent_id, None)
        g["cursors"].pop(agent_id, None)
        self._drop_pending_wakes(agent_id, gid)
        self._save_meta(gid)
        await self._emit_updated(gid)
        return self.summary(gid)

    async def delete(self, gid: str) -> None:
        g = self._require(gid)
        for m in g["members"]:
            self._drop_pending_wakes(m, gid)
        self._groups.pop(gid, None)
        self._turns.pop(gid, None)
        shutil.rmtree(self._dir / gid, ignore_errors=True)
        await self._emit("group.deleted", gid, {"group_id": gid})

    async def take_seat(self, gid: str, agent_id: str) -> dict[str, Any]:
        g = self._require(gid)
        if agent_id not in g["members"]:
            raise GroupError(f"'{agent_id}' is not a member of '{gid}'.")
        g["seats"][agent_id] = SEAT_HUMAN
        self._drop_pending_wakes(agent_id, gid)
        self._save_meta(gid)
        await self._emit_updated(gid)
        return self.summary(gid)

    async def release_seat(self, gid: str, agent_id: str) -> tuple[dict[str, Any], bool]:
        """Hand the seat back to the agent. Returns (summary, redelivered):
        redelivered is True when the agent had unseen turns and was woken."""
        g = self._require(gid)
        if agent_id not in g["members"]:
            raise GroupError(f"'{agent_id}' is not a member of '{gid}'.")
        g["seats"][agent_id] = SEAT_AGENT
        self._save_meta(gid)
        redelivered = False
        if self._unseen(gid, agent_id):
            redelivered = await self._wake(agent_id, gid)
        await self._emit_updated(gid)
        return self.summary(gid), redelivered

    async def post(self, gid: str, sender_id: str,
                   content: str) -> tuple[dict[str, Any], list[str]]:
        """Append a turn and wake every other agent-seated member.

        ``sender_id`` is ADMIN_SENDER for the owner, else a member agent id.
        Returns (turn, woken_agent_ids)."""
        g = self._require(gid)
        content = (content or "").strip()
        if not content:
            raise GroupError("Missing 'content'.")
        if len(content) > MAX_POST_CHARS:
            raise GroupError(
                f"Post too long ({len(content)} chars, max {MAX_POST_CHARS}).")
        if sender_id != ADMIN_SENDER:
            if sender_id not in g["members"]:
                raise GroupError(f"'{sender_id}' is not a member of group '{gid}'.")
            if g["seats"].get(sender_id) == SEAT_HUMAN:
                raise GroupError(
                    f"Your seat in group '{gid}' is held by the owner right now.")
        turn = {"seq": self.last_seq(gid) + 1, "sender_id": sender_id,
                "content": content, "ts": _now()}
        self._append_turn(gid, turn)
        if sender_id != ADMIN_SENDER:
            # The sender has seen its own post; only move forward if it was
            # already caught up, so unseen turns before it still deliver.
            if g["cursors"].get(sender_id, 0) >= turn["seq"] - 1:
                g["cursors"][sender_id] = turn["seq"]
                self._save_meta(gid)
        await self._emit("group.turn", gid, {"group_id": gid, "turn": dict(turn)})
        woken: list[str] = []
        for m in list(g["members"]):
            if m == sender_id or g["seats"].get(m) != SEAT_AGENT:
                continue
            if await self._wake(m, gid):
                woken.append(m)
        return dict(turn), woken

    def mark_read(self, gid: str, agent_id: str) -> None:
        g = self._require(gid)
        if agent_id in g["members"]:
            g["cursors"][agent_id] = self.last_seq(gid)
            self._save_meta(gid)

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    def _unseen(self, gid: str, agent_id: str) -> list[dict[str, Any]]:
        g = self._groups[gid]
        cur = int(g["cursors"].get(agent_id, 0))
        return [t for t in self._turns[gid]
                if int(t["seq"]) > cur and t["sender_id"] != agent_id]

    def render_delivery(self, agent_id: str, gid: str) -> str:
        """Render the turns ``agent_id`` has not seen in ``gid`` and advance
        its cursor. Returns "" when there is nothing new (or the agent is no
        longer an agent-seated member)."""
        g = self._groups.get(gid)
        if g is None or agent_id not in g["members"]:
            return ""
        if g["seats"].get(agent_id) != SEAT_AGENT:
            return ""
        new = self._unseen(gid, agent_id)
        g["cursors"][agent_id] = self.last_seq(gid)
        self._save_meta(gid)
        if not new:
            return ""
        name = g["name"]
        others = [m for m in g["members"] if m != agent_id]
        lines = [
            f"[group {name}] New posts in group '{name}' (group_id {gid}). "
            f"You are {agent_id}; other members: "
            f"{', '.join(others + [ADMIN_SENDER])}."
        ]
        skipped = len(new) - DELIVERY_MAX_TURNS
        if skipped > 0:
            lines.append(
                f"[group {name}] ({skipped} earlier unseen turn(s) not shown; "
                f"call group_read(group_id=\"{gid}\") for the full history.)")
            new = new[-DELIVERY_MAX_TURNS:]
        for t in new:
            lines.append(f"[group {name}] {t['sender_id']}: {t['content']}")
        lines.append(
            f"(You are a peer in this group, not anyone's subordinate. Reply "
            f"with group_post(group_id=\"{gid}\", content=...) only if you have "
            f"something to add. If you have nothing to add, stay silent: end "
            f"your turn without posting (group_pass is optional). Your final "
            f"answer text is NOT posted to the group.)")
        return "\n".join(lines)

    def _running(self, agent_id: str) -> bool:
        rt = self._rt
        if rt is None:
            return False
        try:
            return rt.registry._running_count.get(agent_id, 0) > 0
        except Exception:
            return False

    async def _wake(self, agent_id: str, gid: str) -> bool:
        """Deliver (or schedule delivery of) unseen turns to one member.
        Returns True when a wake was issued or coalesced."""
        rt = self._rt
        if rt is None or rt.get_agent(agent_id) is None:
            return False
        if not self._unseen(gid, agent_id):
            return False
        key = (agent_id, gid)
        if self._running(agent_id):
            if key in self._inflight:
                self._dirty.add(key)
                return True
            self._inflight.add(key)
            task = asyncio.ensure_future(self._inject_loop(agent_id, gid))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return True
        # Idle: one coalesced inbox wake per (agent, group). The turns are
        # rendered when the engine drains it, so later posts ride along.
        for m in rt.inbox.peek(agent_id):
            if (m.data or {}).get(WAKE_KEY) == gid:
                return True
        from .models import InboxMessage, MessagePriority, MessageType
        rt.inbox.post(InboxMessage(
            id=InboxMessage.generate_id(),
            source=f"group:{gid}",
            target=agent_id,
            type=MessageType.WORK,
            priority=MessagePriority.HIGH,
            data={WAKE_KEY: gid},
        ))
        return True

    async def _inject_loop(self, agent_id: str, gid: str) -> None:
        key = (agent_id, gid)
        try:
            while True:
                self._dirty.discard(key)
                text = self.render_delivery(agent_id, gid)
                if text:
                    control = getattr(self._rt, "control", None)
                    delivered = False
                    if control is not None:
                        try:
                            delivered = bool(
                                await control.send_delegate_message(agent_id, text))
                        except Exception:
                            log.debug("group injection failed for %s", agent_id,
                                      exc_info=True)
                    if not delivered:
                        # Nothing took it: re-queue as a plain inbox message.
                        from .models import InboxMessage, MessagePriority, MessageType
                        self._rt.inbox.post(InboxMessage(
                            id=InboxMessage.generate_id(),
                            source=f"group:{gid}", target=agent_id,
                            type=MessageType.WORK, priority=MessagePriority.HIGH,
                            data={"instruction": text},
                        ))
                if key not in self._dirty:
                    break
        finally:
            self._inflight.discard(key)
            self._dirty.discard(key)

    async def wait_idle(self) -> None:
        """Await in-flight live injections (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _drop_pending_wakes(self, agent_id: str, gid: str) -> None:
        rt = self._rt
        if rt is None:
            return
        rt.inbox.remove_where(
            agent_id, lambda m: (m.data or {}).get(WAKE_KEY) == gid)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _emit(self, kind: str, gid: str, data: dict[str, Any]) -> None:
        rt = self._rt
        bus = getattr(rt, "events", None) if rt is not None else None
        if bus is None:
            return
        from .events import Event, EventType
        etype = {
            "group.turn": EventType.GROUP_TURN,
            "group.updated": EventType.GROUP_UPDATED,
            "group.deleted": EventType.GROUP_DELETED,
        }[kind]
        try:
            await bus.emit(Event(type=etype, source=f"group:{gid}", data=data))
        except Exception:
            log.debug("group event emit failed", exc_info=True)

    async def _emit_updated(self, gid: str) -> None:
        await self._emit("group.updated", gid,
                         {"group_id": gid, "group": self.summary(gid)})
