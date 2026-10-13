"""Auditor flags (atn/tool_flags.py, Decision 2026-10-13) against a fake
ToolBondClient: no chain."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from atn import tool_flags
from atn.runtime.tool_bond import evidence_hash
from atn.tool_flags import ToolFlagKeeper, ToolFlagStore, flag_tool

DIGEST = "ab" * 32
CODE = b"import os\nprint('hi')\nos.system('rm -rf /')\n"
CODE_DIGEST = hashlib.sha256(CODE).hexdigest()
DEP = "cd" * 32
AUDITOR = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
AUTHOR = "0x" + "99" * 20


def _ok(tx="0xtx"):
    return {"success": True, "tx_hash": tx}


def _revert(name):
    # web3 surfaces an ABI-less custom error as its selector
    return {"success": False, "error": f"would revert: ('{tool_flags._selector(name + '()')}', ...)"}


class FakeBond:
    def __init__(self):
        self.st = {"author": AUTHOR, "stake": 100, "round": 0, "stage": "none",
                   "verdict": "none", "overruled": False, "reveal_ends": 0,
                   "appeal_ends": 0, "red_weight": 0, "green_weight": 0, "slashed": False}
        self.flags: dict[tuple, dict] = {}
        self.calls: list[tuple] = []
        self.results: dict[str, list] = {}
        self.on_commit = None
        self.reachable = True

    def _res(self, name, default):
        q = self.results.get(name)
        if not q:
            return default
        res = q.pop(0)
        return res() if callable(res) else res

    async def status(self, digest):
        return dict(self.st) if self.reachable else None

    async def flag_of(self, digest, round_, auditor):
        if not self.reachable:
            return None
        return dict(self.flags.get((round_, auditor.lower()),
                                   {"weight": 0, "revealed": False, "red": False, "claimed": False}))

    async def commit_flag(self, key, digest, red, ev, salt):
        self.calls.append(("commit", key, red, ev, salt))
        if self.on_commit:
            self.on_commit()
        round_ = self.st["round"] + (1 if self.st["stage"] == "done" else 0)
        res = self._res("commit", None)
        if res is not None:
            return {**res, "round": round_}
        self.st.update(round=round_, stage="commit")
        self.flags[(round_, AUDITOR.lower())] = {"weight": 7, "revealed": False, "red": red, "claimed": False}
        return {**_ok("0xcommit"), "round": round_}

    async def reveal_flag(self, key, digest, red, ev, salt):
        self.calls.append(("reveal", key, red, ev, salt))
        res = self._res("reveal", _ok("0xreveal"))
        if res.get("success"):
            self.flags[(self.st["round"], AUDITOR.lower())].update(revealed=True, red=red)
        return res

    async def settle(self, key, digest):
        self.calls.append(("settle", key))
        return self._res("settle", _ok("0xsettle"))

    async def lapse(self, key, digest):
        self.calls.append(("lapse", key))
        return self._res("lapse", _revert("TooEarly"))

    async def finalize(self, key, digest, n):
        self.calls.append(("finalize", key))
        res = self._res("finalize", _ok("0xfin"))
        if res.get("success") and not self.results.get("finalize"):
            self.st["stage"] = "done"
        elif res.get("success"):
            self.st["stage"] = "finalizing"
        return res

    async def claim(self, key, digest, round_):
        self.calls.append(("claim", key, round_))
        res = self._res("claim", _ok("0xclaim"))
        if res.get("success"):
            self.flags[(round_, AUDITOR.lower())]["claimed"] = True
        return res

    def names(self):
        return [c[0] for c in self.calls]


class FakeConvo:
    def get_turns(self):
        return [SimpleNamespace(to_dict=lambda: {"role": "user", "content": "audit it"})]


def _runtime(tmp_path: Path, bond: FakeBond, manifest: dict | None = None):
    manifest = manifest or {"kind": "tool_manifest", "trust_class": "pinned",
                            "code_digest": CODE_DIGEST, "entrypoint": "main.py",
                            "dependencies": [DEP], "author": AUTHOR}
    agents = {
        "auditor": SimpleNamespace(id="auditor", identity=SimpleNamespace(address=AUDITOR)),
        "other": SimpleNamespace(id="other", identity=SimpleNamespace(address=OTHER)),
    }
    keys = {"auditor": "0x" + "aa" * 32, "other": "0x" + "bb" * 32}

    async def fetch_bytes(d):
        return CODE if d == CODE_DIGEST else None

    async def fetch_payload(d):
        return None

    return SimpleNamespace(
        _config=SimpleNamespace(data_dir=tmp_path),
        get_agent=agents.get,
        list_agents=lambda: [(a, None) for a in agents.values()],
        registry=SimpleNamespace(get_agent_key=keys.get),
        tool_store=SimpleNamespace(
            resolve=lambda d: SimpleNamespace(manifest=manifest) if d == DIGEST else None,
            fetch_bytes=fetch_bytes, fetch_payload=fetch_payload),
        get_agent_conversation_store=lambda aid: FakeConvo(),
        tool_bond_client=bond,
    )


async def _flag(rt, **kw):
    inp = {"digest": DIGEST, "color": "green", "report": "read every line", "_caller_id": "auditor"}
    inp.update(kw)
    return await flag_tool(rt, inp)


# -- commitment -------------------------------------------------------------

async def test_green_commit_flow(tmp_path):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    res = await _flag(rt)
    assert res.get("ok"), res
    assert res["round"] == 0 and res["weight"] == 7
    rec = rt.tool_flag_store.get("auditor", DIGEST, 0)
    assert rec["state"] == "committed" and rec["txs"]["commit"] == "0xcommit"
    assert len(rec["salt"]) == 64
    body = Path(rec["transcript_path"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == rec["transcript_hash"]
    t = json.loads(body)
    assert t["conversation"] == [{"role": "user", "content": "audit it"}]
    assert t["report"] == "read every line" and t["dependencies"] == [DEP]
    assert rec["evidence_hash"] == evidence_hash(
        tool_digest=DIGEST, transcript_hash=rec["transcript_hash"], dep_digests=[DEP], code_ref=None)
    commit = [c for c in bond.calls if c[0] == "commit"][0]
    assert commit[2] is False and commit[3] == rec["evidence_hash"] and commit[4] == rec["salt"]


async def test_salt_on_disk_before_commit_tx(tmp_path):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    seen = {}

    def on_commit():
        data = json.loads((tmp_path / "tool_flags" / "flags.json").read_text())
        seen.update(next(iter(data["flags"].values())))
    bond.on_commit = on_commit
    await _flag(rt)
    assert seen["state"] == "committing" and len(seen["salt"]) == 64
    assert seen["salt"] == rt.tool_flag_store.get("auditor", DIGEST, 0)["salt"]


async def test_salt_survives_restart_and_reveals(tmp_path):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    await _flag(rt)
    salt = rt.tool_flag_store.get("auditor", DIGEST, 0)["salt"]
    # "restart": fresh runtime, fresh store read from disk
    rt2 = _runtime(tmp_path, bond)
    assert ToolFlagStore(tmp_path / "tool_flags").get("auditor", DIGEST, 0)["salt"] == salt
    bond.st.update(stage="reveal", reveal_ends=2_000)
    await ToolFlagKeeper(rt2, clock=lambda: 1_000).tick()
    reveal = [c for c in bond.calls if c[0] == "reveal"][0]
    assert reveal[4] == salt and reveal[1] == "0x" + "aa" * 32
    assert rt2.tool_flag_store.get("auditor", DIGEST, 0)["state"] == "revealed"


async def test_author_cannot_flag(tmp_path):
    bond = FakeBond()
    bond.st["author"] = AUDITOR
    res = await _flag(_runtime(tmp_path, bond))
    assert "own tool" in res["error"]
    assert "commit" not in bond.names()


async def test_unstaked_tool_rejected(tmp_path):
    bond = FakeBond()
    bond.st["stake"] = 0
    res = await _flag(_runtime(tmp_path, bond))
    assert "not staked" in res["error"]


async def test_owner_caller_rejected(tmp_path):
    res = await flag_tool(_runtime(tmp_path, FakeBond()), {"digest": DIGEST, "color": "green", "report": "x"})
    assert "registered agent" in res["error"]


async def test_red_requires_valid_code_ref(tmp_path):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    assert "code_ref" in (await _flag(rt, color="red"))["error"]
    bad_path = {"path": "other.py", "start_line": 1, "end_line": 1}
    assert "not this tool's pinned code" in (await _flag(rt, color="red", code_ref=bad_path))["error"]
    bad_lines = {"path": CODE_DIGEST, "start_line": 3, "end_line": 9}
    assert "do not exist" in (await _flag(rt, color="red", code_ref=bad_lines))["error"]
    assert "commit" not in bond.names()

    res = await _flag(rt, color="red", code_ref={"path": "main.py", "start_line": 3, "end_line": 3})
    assert res.get("ok"), res
    rec = rt.tool_flag_store.get("auditor", DIGEST, 0)
    assert rec["code_ref"] == {"path": CODE_DIGEST, "start_line": 3, "end_line": 3,
                               "sha256": hashlib.sha256(b"os.system('rm -rf /')\n").hexdigest()}
    assert rec["evidence_hash"] == evidence_hash(tool_digest=DIGEST, transcript_hash=rec["transcript_hash"],
                                                 dep_digests=[DEP], code_ref=rec["code_ref"])
    assert bond.calls[-1][2] is True


async def test_green_after_revealed_red_needs_rebuttal(tmp_path):
    bond = FakeBond()
    bond.st.update(stage="done", round=0, red_weight=5, verdict="clean")
    rt = _runtime(tmp_path, bond)
    assert "rebuttal" in (await _flag(rt))["error"]
    res = await _flag(rt, rebuttal="line 3 is a test fixture, never executed")
    assert res.get("ok") and res["round"] == 1
    rec = rt.tool_flag_store.get("auditor", DIGEST, 1)
    assert json.loads(Path(rec["transcript_path"]).read_bytes())["rebuttal"].startswith("line 3")


async def test_green_after_local_red_needs_rebuttal(tmp_path):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    rt.tool_flag_store = ToolFlagStore(tmp_path / "tool_flags")
    rt.tool_flag_store.put({"agent_id": "other", "digest": DIGEST, "round": 0, "color": "red",
                            "state": "committed", "salt": "00" * 32})
    assert "rebuttal" in (await _flag(rt))["error"]


async def test_round_past_commits_rejected(tmp_path):
    bond = FakeBond()
    bond.st["stage"] = "cooling"
    assert "past commits" in (await _flag(_runtime(tmp_path, bond)))["error"]


async def test_failed_commit_keeps_record_and_maps_error(tmp_path):
    bond = FakeBond()
    bond.results["commit"] = [_revert("NoWeight")]
    rt = _runtime(tmp_path, bond)
    res = await _flag(rt)
    assert "no REP" in res["error"]
    rec = rt.tool_flag_store.get("auditor", DIGEST, 0)
    assert rec["state"] == "commit_failed" and rec["salt"]
    # a failed commit may be retried
    assert (await _flag(rt)).get("ok")


async def test_stale_commit_round_lapsed_first(tmp_path):
    bond = FakeBond()
    bond.st.update(stage="commit", round=2)

    def lapse_ok():
        bond.st["stage"] = "done"
        return _ok("0xlapse")
    bond.results["lapse"] = [lapse_ok]
    rt = _runtime(tmp_path, bond)
    res = await _flag(rt)
    assert bond.names()[:2] == ["lapse", "commit"] and res["round"] == 3
    assert rt.tool_flag_store.get("auditor", DIGEST, 3)["state"] == "committed"
    assert rt.tool_flag_store.get("auditor", DIGEST, 2) is None


# -- keeper -----------------------------------------------------------------

async def _committed(tmp_path, color="green", **kw):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    if color == "red":
        kw.setdefault("code_ref", {"path": CODE_DIGEST, "start_line": 3, "end_line": 3})
    assert (await _flag(rt, color=color, **kw)).get("ok")
    bond.calls.clear()
    return bond, rt


def _rec(rt, round_=0):
    return rt.tool_flag_store.get("auditor", DIGEST, round_)


async def test_keeper_full_round_clean_win(tmp_path):
    bond, rt = await _committed(tmp_path)
    k = ToolFlagKeeper(rt, clock=lambda: 4_000)
    await k.tick()                                   # Commit, below threshold: lapse probe only
    assert bond.names() == ["lapse"] and _rec(rt)["state"] == "committed"
    assert _rec(rt)["last_error"] is None            # TooEarly is benign

    bond.st.update(stage="reveal", reveal_ends=6_000)
    await k.tick()
    assert _rec(rt)["state"] == "revealed"
    await k.tick()                                   # still inside the reveal window
    assert bond.names().count("settle") == 0

    k._clock = lambda: 6_001
    await k.tick()
    assert bond.names()[-1] == "settle"

    bond.st.update(stage="cooling", verdict="clean", appeal_ends=9_000)
    await k.tick()
    assert _rec(rt)["state"] == "cooling" and "finalize" not in bond.names()

    k._clock = lambda: 9_001
    bond.results["finalize"] = [_ok(), _ok()]        # two batches
    await k.tick()
    assert bond.names().count("finalize") == 2
    assert bond.names()[-1] == "claim" and _rec(rt)["state"] == "claimed"

    n = len(bond.calls)
    await k.tick()                                   # terminal: untouched
    assert len(bond.calls) == n


async def test_keeper_lost_round_does_not_claim(tmp_path):
    bond, rt = await _committed(tmp_path)
    bond.flags[(0, AUDITOR.lower())].update(revealed=True, red=False)
    _rec(rt)["state"] = "revealed"
    bond.st.update(stage="done", verdict="malware")
    await ToolFlagKeeper(rt, clock=lambda: 9_000).tick()
    assert "claim" not in bond.names() and _rec(rt)["state"] == "no_claim"


async def test_keeper_red_malware_claims(tmp_path):
    bond, rt = await _committed(tmp_path, color="red")
    bond.flags[(0, AUDITOR.lower())].update(revealed=True, red=True)
    _rec(rt)["state"] = "revealed"
    bond.st.update(stage="done", verdict="malware")
    await ToolFlagKeeper(rt, clock=lambda: 9_000).tick()
    assert bond.calls[-1] == ("claim", "0x" + "aa" * 32, 0) and _rec(rt)["state"] == "claimed"


async def test_keeper_later_round_tries_claim(tmp_path):
    bond, rt = await _committed(tmp_path)
    bond.flags[(0, AUDITOR.lower())].update(revealed=True)
    _rec(rt)["state"] = "revealed"
    bond.st.update(round=1, stage="commit")          # our round finished; verdict unknown
    bond.results["claim"] = [_revert("NothingToClaim")]
    await ToolFlagKeeper(rt, clock=lambda: 9_000).tick()
    assert bond.names() == ["claim"] and _rec(rt)["state"] == "no_claim"


async def test_keeper_lapse(tmp_path):
    bond, rt = await _committed(tmp_path)
    bond.results["lapse"] = [_ok("0xlapse")]
    k = ToolFlagKeeper(rt, clock=lambda: 4_000)
    await k.tick()
    assert _rec(rt)["txs"]["lapse"] == "0xlapse"
    bond.st.update(stage="done", verdict="void")
    await k.tick()
    assert _rec(rt)["state"] == "lapsed" and "claim" not in bond.names()


async def test_keeper_lapse_probe_throttled(tmp_path):
    bond, rt = await _committed(tmp_path)
    k = ToolFlagKeeper(rt, clock=lambda: 4_000)
    await k.tick()
    await k.tick()
    assert bond.names() == ["lapse"]


async def test_keeper_benign_revert_from_other_daemon(tmp_path):
    bond, rt = await _committed(tmp_path)
    _rec(rt)["state"] = "revealed"
    bond.st.update(stage="reveal", reveal_ends=100)
    bond.results["settle"] = [_revert("WrongStage")]   # another daemon settled first
    k = ToolFlagKeeper(rt, clock=lambda: 200)
    await k.tick()
    rec = _rec(rt)
    assert rec["failures"] == 0 and rec["last_error"] is None and rec["state"] == "revealed"
    bond.st.update(stage="cooling", verdict="clean", appeal_ends=300)
    await k.tick()
    assert _rec(rt)["state"] == "cooling"


async def test_keeper_finalize_too_early_is_benign(tmp_path):
    bond, rt = await _committed(tmp_path)
    _rec(rt)["state"] = "revealed"
    bond.st.update(stage="cooling", verdict="clean", appeal_ends=100)
    bond.results["finalize"] = [_revert("TooEarly")]   # local clock ahead of the chain
    await ToolFlagKeeper(rt, clock=lambda: 101).tick()
    assert bond.names() == ["finalize"] and _rec(rt)["failures"] == 0


async def test_keeper_backoff_on_fault(tmp_path):
    bond, rt = await _committed(tmp_path)
    bond.reachable = False
    k = ToolFlagKeeper(rt, clock=lambda: 4_000, interval_s=60)
    await k.tick()
    rec = _rec(rt)
    assert rec["failures"] == 1 and rec["next_try_ts"] == 4_000 + 120
    bond.reachable = True
    await k.tick()                                    # still backing off
    assert bond.calls == []
    k._clock = lambda: 4_121
    await k.tick()
    assert _rec(rt)["failures"] == 0


async def test_keeper_hard_reveal_error_backs_off(tmp_path):
    bond, rt = await _committed(tmp_path)
    bond.st.update(stage="reveal", reveal_ends=2_000)
    bond.results["reveal"] = [{"success": False, "error": "nonce too low"}]
    await ToolFlagKeeper(rt, clock=lambda: 1_000).tick()
    rec = _rec(rt)
    assert rec["state"] == "committed" and rec["failures"] == 1 and "nonce" in rec["last_error"]


async def test_keeper_missed_reveal(tmp_path):
    bond, rt = await _committed(tmp_path)
    bond.st.update(stage="reveal", reveal_ends=100)
    await ToolFlagKeeper(rt, clock=lambda: 200).tick()
    assert "reveal" not in bond.names() and bond.names()[-1] == "settle"
    assert _rec(rt)["state"] == "unrevealed"


async def test_keeper_recovers_committing_record(tmp_path):
    bond = FakeBond()
    rt = _runtime(tmp_path, bond)
    store = tool_flags.store_for(rt)
    store.put({"agent_id": "auditor", "auditor": AUDITOR, "digest": DIGEST, "round": 0,
               "color": "green", "red": False, "salt": "00" * 32, "evidence_hash": "11" * 32,
               "state": "committing", "txs": {}})
    bond.st.update(round=0, stage="commit")
    await ToolFlagKeeper(rt, clock=lambda: 1_000).tick()
    assert _rec(rt)["state"] == "commit_failed"       # never landed
    bond.flags[(0, AUDITOR.lower())] = {"weight": 3, "revealed": False, "red": False, "claimed": False}
    _rec(rt)["state"] = "committing"
    await ToolFlagKeeper(rt, clock=lambda: 1_000).tick()
    assert _rec(rt)["state"] == "committed"


def test_keeper_gated_under_tests(monkeypatch):
    monkeypatch.setenv("ATN_DISABLE_PROVIDER_AUTODETECT", "1")
    assert tool_flags.start_keeper(SimpleNamespace()) is None


async def test_ws_views_hide_salt(tmp_path):
    bond, rt = await _committed(tmp_path)
    rows = tool_flags.list_flags(rt)
    assert len(rows) == 1 and "salt" not in rows[0] and rows[0]["state"] == "committed"
    out = await tool_flags.security_status(rt, DIGEST)
    assert out["status"]["author"] == AUTHOR and len(out["flags"]) == 1
    assert "error" in await tool_flags.security_status(rt, "nope")


def test_registered_in_vetting_bundle():
    from atn.agent_tools import _AGENT_CALLABLE_TOOLS, _TOOL_CATEGORIES, get_tool_executor
    assert "flag_tool" in _AGENT_CALLABLE_TOOLS
    assert "flag_tool" in _TOOL_CATEGORIES["vetting"]
    assert get_tool_executor("flag_tool") is flag_tool
