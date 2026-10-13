"""Auditor flags on staked tools (docs/tool_substrate.md, Decision
2026-10-13: staked tools, flagged audits, REP-settled verdicts).

Three pieces, all daemon-side; the chain surface is ``ToolBondClient``
(atn/runtime/tool_bond.py) over trustless-contracts ToolBond.sol:

- ``flag_tool`` (agent tool): an auditor commits a hidden GREEN ("checked
  for malware, clean") or RED ("this is malicious", citing the exact lines)
  flag. The flag commits to ``evidence_hash(tool, transcript, deps,
  code_ref)``; the transcript is the auditing agent's conversation plus its
  written report, hashed canonically and kept here as appeal evidence.
- ``ToolFlagStore``: per (agent, digest, round) record of color, evidence,
  SALT, transcript path, tx hashes and lifecycle state. Written atomically
  (tmp + fsync + replace) BEFORE the commit tx goes out: a lost salt means
  the auditor cannot reveal and is degraded at finalize.
- ``ToolFlagKeeper``: background loop that walks each stored flag through
  the round: reveal (Reveal stage, own key) -> settle (after revealEnds,
  any key) -> finalize in batches (after the appeal window, any key) ->
  claim (winner, own key); lapse for a Commit round that outlived the
  review window. Another daemon doing a keeper step first is normal:
  WrongStage / TooEarly reverts are benign and the status is re-read.

Commit rounds: the contract binds the commitment to ``bonds.round``, or
``+1`` when that round is Done (the commit opens the next one), which is
what ``ToolBondClient.commit_flag`` computes. A Commit round that already
outlived its review window would be lapsed BY the commit and the flag would
land one round later than its commitment says, so ``flag_tool`` first tries
``lapse`` (free when it would revert) and verifies the landing round after.

"No green flag while a red flag is open without rebutting it" is NOT
enforced on-chain; this module is the only enforcement. Flags are hidden
until reveal, so it enforces what is knowable: revealed red weight in the
tool's current round, or a red flag another agent on THIS daemon holds on
the same round, requires an explicit ``rebuttal`` (stored and folded into
the transcript hash).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .providers.base import ToolDefinition

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 60.0
MAX_BACKOFF_S = 3600.0
# A Commit round's openedAt is not exposed by getRound, so lapse is probed
# (eth_estimateGas, no tx when it would revert) at most this often.
LAPSE_PROBE_S = 3600.0
FINALIZE_BATCH = 50
FINALIZE_MAX_CALLS = 20
TRANSCRIPT_TURN_LIMIT = 200
# A "committing" record younger than this belongs to a flag_tool call still
# waiting on its receipt; the keeper leaves it alone.
COMMIT_GRACE_S = 600.0

# Lifecycle. Terminal states are never touched by the keeper again.
# committing: salt on disk, commit tx not confirmed; unrevealed: the reveal
# window closed before this daemon revealed (the flag will be degraded).
ACTIVE_STATES = ("committing", "committed", "revealed", "unrevealed",
                 "cooling", "done")
TERMINAL_STATES = ("claimed", "no_claim", "lapsed", "commit_failed",
                   "misbound")

# ToolBond custom errors. The v2 ABI carries no error entries, so web3
# surfaces a revert as the raw 4-byte selector; match names and selectors.
_BENIGN_ERRORS = ("WrongStage", "TooEarly")


def _selector(sig: str) -> str:
    try:
        from eth_utils import keccak
        return "0x" + keccak(text=sig).hex()[:8]
    except Exception:  # noqa: BLE001
        return "\x00"  # never matches


def _reverted_with(result: dict[str, Any], *names: str) -> bool:
    if result.get("success"):
        return False
    err = str(result.get("error") or "")
    return any(n in err or _selector(f"{n}()") in err for n in names)


def _benign(result: dict[str, Any]) -> bool:
    return _reverted_with(result, *_BENIGN_ERRORS)


def _norm_digest(d: Any) -> str:
    return str(d or "").strip().lower().removeprefix("0x")


def _is_digest(d: str) -> bool:
    return len(d) == 64 and all(c in "0123456789abcdef" for c in d)


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def client_for(runtime: Any):
    """The runtime's ToolBondClient (tests pre-set ``tool_bond_client``)."""
    client = getattr(runtime, "tool_bond_client", None)
    if client is not None:
        return client
    from .runtime.tool_bond import ToolBondClient
    return ToolBondClient(runtime)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ToolFlagStore:
    """``<data_dir>/tool_flags/flags.json`` + ``transcripts/<hash>.json``.

    Record key ``"<agent_id>:<digest>:<round>"``. Every mutation persists
    atomically with an fsync; the salt is the one field that can never be
    regenerated.
    """

    def __init__(self, root: Path) -> None:
        self._dir = Path(root)
        self._path = self._dir / "flags.json"
        self._transcripts = self._dir / "transcripts"
        self._flags: dict[str, dict[str, Any]] = self._load()

    @staticmethod
    def key(agent_id: str, digest: str, round_: int) -> str:
        return f"{agent_id}:{digest}:{int(round_)}"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            flags = data.get("flags") if isinstance(data, dict) else None
            if isinstance(flags, dict):
                return {str(k): dict(v) for k, v in flags.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError) as exc:
            # Never overwrite an unreadable store: it holds salts.
            log.error("tool flag store unreadable (%s); refusing to start empty", exc)
            raise
        return {}

    def save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(json.dumps({"version": 1, "flags": self._flags},
                                sort_keys=True, indent=1).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    def put(self, rec: dict[str, Any]) -> None:
        rec["updated_ts"] = int(time.time())
        self._flags[self.key(rec["agent_id"], rec["digest"], rec["round"])] = rec
        self.save()

    def rekey(self, rec: dict[str, Any], new_round: int) -> None:
        old = self.key(rec["agent_id"], rec["digest"], rec["round"])
        self._flags.pop(old, None)
        rec["round"] = int(new_round)
        self.put(rec)

    def get(self, agent_id: str, digest: str, round_: int) -> Optional[dict[str, Any]]:
        return self._flags.get(self.key(agent_id, digest, round_))

    def all(self) -> list[dict[str, Any]]:
        return list(self._flags.values())

    def for_digest(self, digest: str) -> list[dict[str, Any]]:
        return [r for r in self._flags.values() if r.get("digest") == digest]

    def write_transcript(self, body: bytes) -> tuple[str, str]:
        """Persist the exact hashed bytes; returns (sha256 hex, path)."""
        h = hashlib.sha256(body).hexdigest()
        self._transcripts.mkdir(parents=True, exist_ok=True)
        path = self._transcripts / f"{h}.json"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        return h, str(path)


def store_for(runtime: Any) -> ToolFlagStore:
    store = getattr(runtime, "tool_flag_store", None)
    if isinstance(store, ToolFlagStore):
        return store
    store = ToolFlagStore(Path(runtime._config.data_dir) / "tool_flags")
    try:
        setattr(runtime, "tool_flag_store", store)
    except Exception:  # noqa: BLE001
        pass
    return store


def public_view(rec: dict[str, Any]) -> dict[str, Any]:
    """A record as the owner UI sees it: never the salt."""
    return {k: v for k, v in rec.items() if k != "salt"}


# ---------------------------------------------------------------------------
# flag_tool
# ---------------------------------------------------------------------------

FLAG_TOOL_DEFINITION = ToolDefinition(
    name="flag_tool",
    description=(
        "Raise a malware-audit flag on a STAKED published tool (validator "
        "role, vetting bundle). Read the pinned code first (vet_tool with "
        "only a digest). GREEN means 'I checked this for malware and it is "
        "clean'. RED means 'this is malicious' and must cite the exact "
        "malicious lines of the pinned code in code_ref. The flag is "
        "committed hidden on-chain and revealed automatically by this "
        "daemon once enough reputation has flagged; it weighs your REP "
        "(zero REP cannot flag). Majority flaggers are rewarded, minority "
        "and unrevealed flaggers lose REP, so flag only what you verified. "
        "Your conversation and report are hashed into the flag and kept "
        "locally as appeal evidence. You cannot flag your own tool. A green "
        "flag on a tool with an open red flag needs an explicit rebuttal."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "digest": {"type": "string", "description": "64-hex manifest digest of the staked tool."},
            "color": {"type": "string", "enum": ["green", "red"]},
            "report": {"type": "string", "description": "What you checked and what you found."},
            "code_ref": {
                "type": "object",
                "description": (
                    "Required for red: the malicious lines in the pinned code. "
                    "path is the code digest (the tool's single code blob; its "
                    "entrypoint name is also accepted); lines are 1-based, inclusive."
                ),
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
            "rebuttal": {"type": "string", "description": "Required for green when a red flag is open: why the red flag is wrong."},
        },
        "required": ["digest", "color", "report"],
    },
)


async def _resolve_manifest(runtime: Any, digest: str) -> Optional[dict[str, Any]]:
    tool_store = getattr(runtime, "tool_store", None)
    if tool_store is None:
        return None
    rec = tool_store.resolve(digest) if hasattr(tool_store, "resolve") else None
    if rec is not None:
        return rec.manifest
    try:
        payload = await tool_store.fetch_payload(digest)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(payload, dict) and payload.get("kind") == "tool_manifest":
        return payload
    return None


def verify_code_ref(code_ref: Any, manifest: dict[str, Any],
                    code_raw: bytes) -> dict[str, Any] | str:
    """Normalized code_ref {path, start_line, end_line, sha256} or an error.

    Lines are 1-based inclusive over ``code_raw.splitlines(keepends=True)``;
    ``sha256`` is over the exact cited bytes, so anyone holding the code
    blob can recompute it.
    """
    if not isinstance(code_ref, dict):
        return "a red flag requires code_ref {path, start_line, end_line}"
    code_digest = str(manifest.get("code_digest") or "")
    path = str(code_ref.get("path") or "").strip()
    accepted = {code_digest, f"{code_digest}.py"}
    if manifest.get("entrypoint"):
        accepted.add(str(manifest["entrypoint"]))
    if path not in accepted:
        return (f"code_ref.path {path!r} is not this tool's pinned code; use "
                f"the code digest {code_digest}")
    try:
        start, end = int(code_ref.get("start_line")), int(code_ref.get("end_line"))
    except (TypeError, ValueError):
        return "code_ref start_line/end_line must be integers"
    lines = code_raw.splitlines(keepends=True)
    if start < 1 or end < start or end > len(lines):
        return (f"code_ref lines {start}-{end} do not exist in the pinned code "
                f"({len(lines)} lines)")
    cited = b"".join(lines[start - 1:end])
    return {"path": code_digest, "start_line": start, "end_line": end,
            "sha256": hashlib.sha256(cited).hexdigest()}


def _conversation(runtime: Any, agent_id: str) -> list[dict[str, Any]]:
    try:
        store = runtime.get_agent_conversation_store(agent_id)
        turns = store.get_turns()[-TRANSCRIPT_TURN_LIMIT:]
        return [t.to_dict() for t in turns]
    except Exception:  # noqa: BLE001
        log.debug("no conversation store for %s", agent_id, exc_info=True)
        return []


def build_transcript(*, agent_id: str, auditor: str, digest: str,
                     manifest: dict[str, Any], color: str, report: str,
                     rebuttal: str, code_ref: Optional[dict[str, Any]],
                     conversation: list[dict[str, Any]], ts: int) -> bytes:
    """Canonical transcript bytes: what the flag's transcript hash covers."""
    return _canonical({
        "kind": "tool_flag_transcript", "v": 1,
        "agent_id": agent_id, "auditor": auditor.lower(),
        "tool_digest": digest,
        "code_digest": str(manifest.get("code_digest") or ""),
        "dependencies": sorted(_norm_digest(d) for d in manifest.get("dependencies") or []),
        "color": color, "report": report, "rebuttal": rebuttal or None,
        "code_ref": code_ref, "conversation": conversation, "ts": int(ts),
    })


async def flag_tool(runtime: "Runtime", input: dict[str, Any]) -> dict[str, Any]:
    """Executor for ``flag_tool`` (see FLAG_TOOL_DEFINITION)."""
    from .agent_tools import _caller_identity
    from .runtime.tool_bond import evidence_hash

    caller_id = input.get("_caller_id")
    resolved = _caller_identity(runtime, caller_id)
    if isinstance(resolved, dict):
        return resolved
    _defn, identity, key = resolved
    auditor = str(identity.address)

    digest = _norm_digest(input.get("digest"))
    if not _is_digest(digest):
        return {"error": "digest must be a 64-hex manifest digest"}
    color = str(input.get("color") or "").strip().lower()
    if color not in ("green", "red"):
        return {"error": "color must be 'green' or 'red'"}
    report = str(input.get("report") or "").strip()
    if not report:
        return {"error": "a flag requires a report: what you checked and what you found"}
    rebuttal = str(input.get("rebuttal") or "").strip()

    manifest = await _resolve_manifest(runtime, digest)
    if manifest is None:
        return {"error": f"cannot resolve manifest {digest[:16]}: not registered "
                         "here and not fetchable from the network"}
    if str(manifest.get("trust_class") or "") != "pinned":
        return {"error": "only pinned tools carry code to audit"}

    client = client_for(runtime)
    st = await client.status(digest)
    if st is None:
        return {"error": "ToolBond unreachable or not configured; try again later"}
    if str(st.get("author") or "").lower() == auditor.lower():
        return {"error": "you cannot flag your own tool"}
    if int(st.get("stake") or 0) == 0 or st.get("slashed"):
        return {"error": "tool is not staked on ToolBond (or was slashed): nothing to flag"}
    if st.get("stage") in ("reveal", "cooling", "finalizing"):
        return {"error": f"the current audit round is past commits (stage "
                         f"{st['stage']}); flag again once it is done"}

    code_ref: Optional[dict[str, Any]] = None
    if color == "red":
        code_digest = str(manifest.get("code_digest") or "")
        code_raw = await runtime.tool_store.fetch_bytes(code_digest) if code_digest else None
        if code_raw is None:
            return {"error": "pinned code blob unavailable; cannot verify code_ref"}
        checked = verify_code_ref(input.get("code_ref"), manifest, code_raw)
        if isinstance(checked, str):
            return {"error": checked}
        code_ref = checked

    store = store_for(runtime)
    round_ = int(st["round"]) + (1 if st.get("stage") == "done" else 0)

    if color == "green" and not rebuttal:
        local_red = [r for r in store.for_digest(digest)
                     if r.get("color") == "red" and int(r.get("round", -1)) == round_
                     and r.get("state") not in ("commit_failed", "misbound")]
        if int(st.get("red_weight") or 0) > 0 or local_red:
            return {"error": "a red flag is open on this tool; a green flag must "
                             "include an explicit 'rebuttal' of it"}

    existing = store.get(str(caller_id), digest, round_)
    if existing is not None and existing.get("state") not in ("commit_failed",):
        return {"error": f"you already flagged this tool in round {round_} "
                         f"({existing.get('color')}, {existing.get('state')})"}

    now = int(time.time())
    body = build_transcript(
        agent_id=str(caller_id), auditor=auditor, digest=digest, manifest=manifest,
        color=color, report=report, rebuttal=rebuttal, code_ref=code_ref,
        conversation=_conversation(runtime, str(caller_id)), ts=now)
    transcript_hash, transcript_path = store.write_transcript(body)
    deps = [_norm_digest(d) for d in manifest.get("dependencies") or []]
    ev = evidence_hash(tool_digest=digest, transcript_hash=transcript_hash,
                       dep_digests=deps, code_ref=code_ref)
    rec: dict[str, Any] = {
        "agent_id": str(caller_id), "auditor": auditor, "digest": digest,
        "round": round_, "color": color, "red": color == "red",
        "report": report, "rebuttal": rebuttal or None, "code_ref": code_ref,
        "dep_digests": deps, "transcript_hash": transcript_hash,
        "transcript_path": transcript_path, "evidence_hash": ev,
        "salt": secrets.token_hex(32), "state": "committing", "txs": {},
        "created_ts": now, "verdict": None, "last_error": None,
        "failures": 0, "next_try_ts": 0,
    }
    # Salt hits disk before the commit can reach the chain.
    store.put(rec)

    if st.get("stage") == "commit":
        # A stale Commit round would be lapsed by our own commit, landing it
        # one round past what the commitment binds. Lapse it first; this is
        # an estimate-only no-op while the round is still live.
        await client.lapse(key, digest)
        st2 = await client.status(digest)
        if st2 is not None and st2.get("stage") == "done":
            store.rekey(rec, int(st2["round"]) + 1)

    res = await client.commit_flag(key, digest, color == "red", ev, rec["salt"])
    if not res.get("success"):
        rec["state"] = "commit_failed"
        rec["last_error"] = str(res.get("error") or "commit failed")
        store.put(rec)
        err = rec["last_error"]
        if _reverted_with(res, "NotAuthor"):
            err = "you cannot flag your own tool"
        elif _reverted_with(res, "NoWeight"):
            err = "you hold no REP at the round snapshot: a zero-REP flag weighs nothing"
        elif _reverted_with(res, "AlreadyFlagged"):
            err = "you already flagged this tool in the current round"
        return {"error": f"commit failed: {err}"}

    landed = int(res.get("round", rec["round"]))
    if landed != rec["round"]:
        store.rekey(rec, landed)
    rec["txs"]["commit"] = res.get("tx_hash")
    rec["state"] = "committed"
    store.put(rec)
    flag = await client.flag_of(digest, landed, auditor)
    if flag is not None and int(flag.get("weight") or 0) == 0:
        rec["state"] = "misbound"
        rec["last_error"] = ("commit landed outside the round its commitment "
                             "binds (round moved during the commit); it cannot "
                             "be revealed. Flag again.")
        store.put(rec)
        return {"error": rec["last_error"], "commit_tx": res.get("tx_hash")}
    return {
        "ok": True, "digest": digest, "round": landed, "color": color,
        "state": rec["state"], "commit_tx": res.get("tx_hash"),
        "evidence_hash": ev, "transcript_hash": transcript_hash,
        "weight": int(flag.get("weight") or 0) if flag else None,
        "note": ("committed hidden; this daemon reveals it when the round reaches "
                 "its REP threshold, then settles, finalizes and claims"),
    }


# ---------------------------------------------------------------------------
# Keeper
# ---------------------------------------------------------------------------

def keeper_disabled() -> bool:
    return (os.environ.get("ATN_DISABLE_PROVIDER_AUTODETECT", "").strip() == "1"
            or os.environ.get("ATN_DISABLE_TOOL_FLAG_KEEPER", "").strip() == "1")


class ToolFlagKeeper:
    """Walks this daemon's stored flags through their rounds. One ``tick``
    is idempotent and never raises; ``run`` loops it until cancelled."""

    def __init__(self, runtime: Any, *, client: Any = None,
                 clock: Callable[[], float] = time.time,
                 interval_s: float = POLL_INTERVAL_S) -> None:
        self._runtime = runtime
        self._client = client
        self._clock = clock
        self._interval = interval_s

    @property
    def client(self):
        return self._client if self._client is not None else client_for(self._runtime)

    @property
    def store(self) -> ToolFlagStore:
        return store_for(self._runtime)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("tool flag keeper tick failed")
            await asyncio.sleep(self._interval)

    # -- keys ------------------------------------------------------------
    def _own_key(self, rec: dict[str, Any]) -> Optional[str]:
        try:
            return self._runtime.registry.get_agent_key(rec["agent_id"])
        except Exception:  # noqa: BLE001
            return None

    def _gas_key(self, rec: dict[str, Any]) -> Optional[str]:
        """Permissionless calls: the flagger's key, else any agent key here."""
        key = self._own_key(rec)
        if key:
            return key
        try:
            for defn, _status in self._runtime.list_agents():
                k = self._runtime.registry.get_agent_key(defn.id)
                if k:
                    return k
        except Exception:  # noqa: BLE001
            pass
        return None

    # -- bookkeeping -----------------------------------------------------
    def _fault(self, rec: dict[str, Any], err: str) -> None:
        rec["failures"] = int(rec.get("failures") or 0) + 1
        rec["last_error"] = err
        rec["next_try_ts"] = self._clock() + min(
            self._interval * (2 ** rec["failures"]), MAX_BACKOFF_S)
        self.store.put(rec)

    def _ok(self, rec: dict[str, Any], state: Optional[str] = None) -> None:
        if state:
            rec["state"] = state
        rec["failures"] = 0
        rec["next_try_ts"] = 0
        rec["last_error"] = None
        self.store.put(rec)

    async def tick(self) -> None:
        now = self._clock()
        for rec in self.store.all():
            if rec.get("state") in TERMINAL_STATES:
                continue
            if float(rec.get("next_try_ts") or 0) > now:
                continue
            if (rec.get("state") == "committing"
                    and now - float(rec.get("created_ts") or 0) < COMMIT_GRACE_S):
                continue
            try:
                await self.step(rec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("flag keeper step failed for %s: %s", rec.get("digest", "")[:16], exc)
                self._fault(rec, str(exc))

    async def step(self, rec: dict[str, Any]) -> None:
        client = self.client
        digest, round_ = rec["digest"], int(rec["round"])
        st = await client.status(digest)
        if st is None:
            self._fault(rec, "ToolBond unreachable")
            return

        if rec.get("state") == "committing":
            # Crashed between persisting and the commit landing: ask the chain.
            flag = await client.flag_of(digest, round_, rec["auditor"])
            if flag is None:
                self._fault(rec, "ToolBond unreachable")
                return
            if int(flag.get("weight") or 0) == 0:
                self._ok(rec, "commit_failed")
                rec["last_error"] = "commit never landed"
                self.store.put(rec)
                return
            self._ok(rec, "committed")

        if int(st["round"]) > round_:
            # Rounds only advance past Done: ours is finished.
            await self._claim(rec, verdict=None)
            return

        stage = st.get("stage")
        now = self._clock()
        if stage == "commit":
            last = float(rec.get("lapse_probe_ts") or 0)
            if now - last >= LAPSE_PROBE_S:
                rec["lapse_probe_ts"] = now
                await self._keeper_call(rec, "lapse", lambda k: client.lapse(k, digest))
            return
        if stage == "reveal":
            reveal_ends = int(st.get("reveal_ends") or 0)
            if rec["state"] == "committed" and now <= reveal_ends:
                await self._reveal(rec)
                return
            if rec["state"] == "committed":
                rec["state"] = "unrevealed"
                rec["last_error"] = "reveal window passed before this daemon revealed"
                self.store.put(rec)
            if now > reveal_ends:
                await self._keeper_call(rec, "settle", lambda k: client.settle(k, digest))
            return
        if stage in ("cooling", "finalizing"):
            rec["verdict"] = st.get("verdict")
            if rec["state"] == "committed":
                rec["state"] = "unrevealed"
            if rec["state"] == "revealed":
                rec["state"] = "cooling"
            self.store.put(rec)
            if stage == "finalizing" or st.get("overruled") or now > int(st.get("appeal_ends") or 0):
                await self._finalize(rec)
            return
        if stage == "done":
            await self._claim(rec, verdict=st.get("verdict"))

    async def _keeper_call(self, rec: dict[str, Any], name: str,
                           send: Callable[[str], Any]) -> Optional[dict[str, Any]]:
        key = self._gas_key(rec)
        if not key:
            self._fault(rec, f"no agent key on this daemon to send {name}")
            return None
        res = await send(key)
        if res.get("success"):
            rec.setdefault("txs", {})[name] = res.get("tx_hash")
            self._ok(rec)
        elif _benign(res):
            # Someone else moved the round, or it is not time yet.
            self._ok(rec)
        else:
            self._fault(rec, f"{name}: {res.get('error')}")
        return res

    async def _reveal(self, rec: dict[str, Any]) -> None:
        key = self._own_key(rec)
        if not key:
            self._fault(rec, "flagging agent's key is gone; cannot reveal")
            return
        res = await self.client.reveal_flag(key, rec["digest"], bool(rec["red"]),
                                            rec["evidence_hash"], rec["salt"])
        if res.get("success"):
            rec.setdefault("txs", {})["reveal"] = res.get("tx_hash")
            self._ok(rec, "revealed")
            return
        if _reverted_with(res, "BadReveal"):
            flag = await self.client.flag_of(rec["digest"], int(rec["round"]), rec["auditor"])
            if flag is not None and flag.get("revealed"):
                self._ok(rec, "revealed")  # an earlier reveal landed
                return
        if _benign(res):
            self.store.put(rec)
            return
        self._fault(rec, f"reveal: {res.get('error')}")

    async def _finalize(self, rec: dict[str, Any]) -> None:
        client, digest = self.client, rec["digest"]
        for _ in range(FINALIZE_MAX_CALLS):
            res = await self._keeper_call(
                rec, "finalize", lambda k: client.finalize(k, digest, FINALIZE_BATCH))
            if res is None or not (res.get("success") or _benign(res)):
                return
            st = await client.status(digest)
            if st is None:
                self._fault(rec, "ToolBond unreachable")
                return
            if int(st["round"]) != int(rec["round"]) or st.get("stage") == "done":
                rec["state"] = "done"
                same = int(st["round"]) == int(rec["round"])
                self.store.put(rec)
                await self._claim(rec, verdict=st.get("verdict") if same else None)
                return
            if st.get("stage") == "cooling":
                return  # still inside the appeal window (TooEarly)

    async def _claim(self, rec: dict[str, Any], verdict: Optional[str]) -> None:
        if verdict is not None:
            rec["verdict"] = verdict
        v = rec.get("verdict")
        flag = await self.client.flag_of(rec["digest"], int(rec["round"]), rec["auditor"])
        if flag is None:
            self._fault(rec, "ToolBond unreachable")
            return
        if flag.get("claimed"):
            self._ok(rec, "claimed")
            return
        if not flag.get("revealed"):
            # Never revealed: a lapsed round (no penalty) or a degraded flag.
            lapsed = rec["state"] == "committed" and v in (None, "void")
            self._ok(rec, "lapsed" if lapsed else "no_claim")
            return
        if v is not None:
            won = (v == "malware" and flag.get("red")) or (v == "clean" and not flag.get("red"))
            if not won:
                self._ok(rec, "no_claim")
                return
        key = self._own_key(rec)
        if not key:
            self._fault(rec, "flagging agent's key is gone; cannot claim")
            return
        res = await self.client.claim(key, rec["digest"], int(rec["round"]))
        if res.get("success"):
            rec.setdefault("txs", {})["claim"] = res.get("tx_hash")
            self._ok(rec, "claimed")
        elif _reverted_with(res, "NothingToClaim"):
            self._ok(rec, "no_claim")
        elif _benign(res):
            self.store.put(rec)
        else:
            self._fault(rec, f"claim: {res.get('error')}")


def start_keeper(runtime: Any) -> Optional[asyncio.Task]:
    """Started by Runtime.start; None under the test gate."""
    if keeper_disabled():
        return None
    return asyncio.create_task(ToolFlagKeeper(runtime).run())


# ---------------------------------------------------------------------------
# Owner UI reads (ws_server: tool_flags_list / tool_security_status)
# ---------------------------------------------------------------------------

def list_flags(runtime: Any, digest: str = "") -> list[dict[str, Any]]:
    d = _norm_digest(digest)
    rows = store_for(runtime).all()
    if d:
        rows = [r for r in rows if r.get("digest") == d]
    rows.sort(key=lambda r: int(r.get("created_ts") or 0), reverse=True)
    return [public_view(r) for r in rows]


async def security_status(runtime: Any, digest: str) -> dict[str, Any]:
    d = _norm_digest(digest)
    if not _is_digest(d):
        return {"error": "digest must be a 64-hex manifest digest"}
    status = await client_for(runtime).status(d)
    return {"digest": d, "status": status, "flags": list_flags(runtime, d)}
