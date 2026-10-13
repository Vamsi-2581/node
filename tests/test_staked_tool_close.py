"""Staked tools at the federated close (docs/tool_substrate.md, Decision
2026-10-13): the auditor share of vetted tools' mints, the reviewed
composition split (own_share), and successor merit carry. Plus the pure
ToolBond log fold that builds the pinned vetting snapshot, and
determinism of all of it under input reordering.
"""

from __future__ import annotations

import copy
import math
import random
from typing import Any, Dict, List

import pytest

from nodes.common.canonical_ordering import canonical_order
from nodes.common.event_gossip import Keypair
from nodes.common.federated_reconcile import (
    AUDITOR_SHARE_BPS_DEFAULT,
    COMPOSITE_ROOT_SHARE,
    compute_tool_mint,
    federated_epoch_close,
)
from nodes.common.voice_state import tool_vetting_snapshot_from_logs
from nodes.common.world_model_substrate.adapter import build_charter_world
from nodes.common.world_model_substrate.aggregate import apply_events
from nodes.common.world_model_substrate.events import ToolUsed
from nodes.common.world_model_substrate.tool_usage import tool_usage_from_events

from tests.test_tool_mint import (
    AUTHOR,
    DIGEST,
    EMBED_DIM,
    _batches,
    _receipt_event,
    _registration_event,
)

DEP = "cc" * 32
SUCC = "dd" * 32
AUD_A = "0x" + "a1" * 20
AUD_B = "0x" + "b2" * 20
REGS = {DIGEST: {"trust_class": "pinned", "author": AUTHOR}}


def _world(*events):
    world = build_charter_world(embedding_dim=EMBED_DIM)
    apply_events(world, list(events) or [_registration_event(1)],
                 equilibrate_after=False)
    return world


def _snapshot(auditors: Dict[str, int], *, digest: str = DIGEST,
              bps: Any = 4000) -> Dict[str, Any]:
    snap: Dict[str, Any] = {"tools": {digest: {"round": 0,
                                               "auditors": dict(auditors)}}}
    if bps is not None:
        snap["auditor_share_bps"] = bps
    return snap


def _agent_totals(out: Dict[str, Any]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for bucket in out["node_agent"].values():
        for agent, amount in bucket.items():
            totals[agent] = totals.get(agent, 0.0) + amount
    return totals


def _receipts(n: int = 2) -> List[Dict[str, Any]]:
    return [_receipt_event(i, f"caller-{i}") for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# 1. Auditor share
# ---------------------------------------------------------------------------


class TestAuditorShare:
    def test_redistributes_without_changing_total(self):
        base = compute_tool_mint(_world(), _receipts(), registrations=REGS)
        split = compute_tool_mint(
            _world(), _receipts(), registrations=REGS,
            vetting_snapshot=_snapshot({AUD_A: 10**18}),
        )
        mint = base["per_digest"][DIGEST]["mint"]
        totals = _agent_totals(split)
        assert sum(totals.values()) == pytest.approx(mint, rel=1e-12)
        assert totals[AUTHOR] == pytest.approx(mint * 0.6, rel=1e-12)
        assert totals[AUD_A] == pytest.approx(mint * 0.4, rel=1e-12)
        entry = split["per_digest"][DIGEST]
        assert entry["mint"] == pytest.approx(mint)
        assert entry["vetted"] is True
        assert entry["auditor_mint"] == pytest.approx(mint * 0.4)

    def test_split_by_flag_weight(self):
        out = compute_tool_mint(
            _world(), _receipts(), registrations=REGS,
            vetting_snapshot=_snapshot({AUD_A: 3 * 10**18, AUD_B: 10**18},
                                       bps=5000),
        )
        mint = out["per_digest"][DIGEST]["mint"]
        totals = _agent_totals(out)
        assert totals[AUD_A] == pytest.approx(mint * 0.5 * 0.75, rel=1e-12)
        assert totals[AUD_B] == pytest.approx(mint * 0.5 * 0.25, rel=1e-12)
        assert totals[AUTHOR] == pytest.approx(mint * 0.5, rel=1e-12)

    def test_default_bps_when_snapshot_omits_it(self):
        out = compute_tool_mint(
            _world(), _receipts(), registrations=REGS,
            vetting_snapshot=_snapshot({AUD_A: 1}, bps=None),
        )
        mint = out["per_digest"][DIGEST]["mint"]
        assert _agent_totals(out)[AUD_A] == pytest.approx(
            mint * AUDITOR_SHARE_BPS_DEFAULT / 10_000, rel=1e-12)

    def test_unvetted_tool_mints_exactly_as_before(self):
        base = compute_tool_mint(_world(), _receipts(), registrations=REGS)
        other = compute_tool_mint(
            _world(), _receipts(), registrations=REGS,
            vetting_snapshot=_snapshot({AUD_A: 10**18}, digest="ef" * 32),
        )
        assert other["node_agent"] == base["node_agent"]
        assert other["per_digest"][DIGEST]["vetted"] is False
        assert other["per_digest"][DIGEST]["auditor_mint"] == 0.0

    def test_auditor_maps_onto_known_agent_spelling(self):
        """A 0x auditor that is also a caller under a checksum spelling is
        credited under THAT id (one address, one mint leaf)."""
        checksum = "0x" + "A1" * 20
        events = _receipts() + [_receipt_event(9, checksum)]
        out = compute_tool_mint(
            _world(), events, registrations=REGS,
            vetting_snapshot=_snapshot({AUD_A: 1}),
        )
        totals = _agent_totals(out)
        assert checksum in totals and AUD_A not in totals

    def test_pool_conservation_through_close(self):
        pool = 7.123457
        kp_a, kp_c = Keypair.generate(), Keypair.generate()
        batches = _batches([_registration_event(1)], kp_a) + _batches(
            _receipts(3), kp_c)
        snap = _snapshot({AUD_A: 2, AUD_B: 1})
        plain = federated_epoch_close(canonical_order(batches),
                                      emission_pool=pool)
        split = federated_epoch_close(canonical_order(batches),
                                      emission_pool=pool,
                                      vetting_snapshot=snap)
        units = {a: round(v * 1_000_000) for a, v in split["agent_mint"].items()}
        pool_units = round(pool * 1_000_000)
        assert set(units) == {AUTHOR, AUD_A, AUD_B}
        assert sum(units.values()) <= pool_units
        # Flooring dust is at most one base unit per recipient.
        assert pool_units - sum(units.values()) < len(units)
        assert sum(split["agent_mint"].values()) == pytest.approx(
            sum(plain["agent_mint"].values()), abs=3e-6)
        assert units[AUD_A] == pytest.approx(2 * units[AUD_B], abs=1)
        assert split["tool_mint"][DIGEST]["auditor_mint"] == pytest.approx(
            pool * 0.4, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. ToolBond log fold (the pinned snapshot builder)
# ---------------------------------------------------------------------------


def _rec(block: int, name: str, **args) -> Dict[str, Any]:
    return {"block": block, "index": 0, "event": name, "args": args}


D = bytes.fromhex(DIGEST)


def _params(block: int = 1, *, min_stake: int = 100, bps: int = 3000):
    return _rec(block, "ParamsSet", minStake=min_stake, thresholdBps=500,
                reviewWindow=1, revealWindow=1, finderBps=5000, degradeBps=100,
                authorSlashBps=10_000, unbondDelay=1, auditorShareBps=bps)


def _clean_round(block: int, rnd: int = 0) -> List[Dict[str, Any]]:
    return [
        _rec(block, "FlagCommitted", digest=D, round=rnd, auditor=AUD_A, weight=30),
        _rec(block, "FlagCommitted", digest=D, round=rnd, auditor=AUD_B, weight=10),
        _rec(block, "FlagCommitted", digest=D, round=rnd,
             auditor="0x" + "c3" * 20, weight=5),
        _rec(block + 1, "FlagRevealed", digest=D, round=rnd, auditor=AUD_A, red=False,
             evidenceHash=b"\x01" * 32),
        _rec(block + 1, "FlagRevealed", digest=D, round=rnd, auditor=AUD_B, red=True,
             evidenceHash=b"\x02" * 32),
        # c3 committed but never revealed: not an auditor.
        _rec(block + 2, "VerdictReached", digest=D, round=rnd, verdict=1, appealEnds=9),
        _rec(block + 3, "Finalized", digest=D, round=rnd, verdict=1, repPool=0, atnPool=0),
    ]


class TestVettingSnapshotFold:
    def _bonded(self):
        return [_params(), _rec(2, "Bonded", digest=D, author=AUD_B, amount=100,
                                stake=100)]

    def test_clean_round_lists_green_revealers_with_weights(self):
        snap = tool_vetting_snapshot_from_logs(self._bonded() + _clean_round(3))
        assert snap["auditor_share_bps"] == 3000
        assert snap["tools"] == {DIGEST: {"round": 0, "auditors": {AUD_A: 30}}}

    def test_default_bps_without_params_event(self):
        recs = [_rec(2, "Bonded", digest=D, author=AUD_B, amount=1, stake=1)]
        snap = tool_vetting_snapshot_from_logs(recs + _clean_round(3))
        assert snap["auditor_share_bps"] == AUDITOR_SHARE_BPS_DEFAULT
        assert DIGEST in snap["tools"]

    def test_never_clean_is_not_vetted(self):
        snap = tool_vetting_snapshot_from_logs(self._bonded())
        assert snap["tools"] == {}

    def test_pending_malware_verdict_unvets(self):
        recs = self._bonded() + _clean_round(3) + [
            _rec(10, "FlagCommitted", digest=D, round=1, auditor=AUD_B, weight=50),
            _rec(11, "FlagRevealed", digest=D, round=1, auditor=AUD_B, red=True,
                 evidenceHash=b"\x03" * 32),
            _rec(12, "VerdictReached", digest=D, round=1, verdict=2, appealEnds=99),
        ]
        assert tool_vetting_snapshot_from_logs(recs)["tools"] == {}
        # DAO overrules to Clean: vetted again, still round 0's auditors
        # until round 1 finalizes.
        recs.append(_rec(13, "Overruled", digest=D, round=1, verdict=1))
        assert tool_vetting_snapshot_from_logs(recs)["tools"][DIGEST]["round"] == 0

    def test_unbond_and_min_stake_raise_unvet(self):
        unbond = self._bonded() + _clean_round(3) + [
            _rec(10, "UnbondRequested", digest=D, availableAt=5)]
        assert tool_vetting_snapshot_from_logs(unbond)["tools"] == {}
        raised = self._bonded() + _clean_round(3) + [_params(10, min_stake=500)]
        assert tool_vetting_snapshot_from_logs(raised)["tools"] == {}

    def test_later_void_round_keeps_clean_auditors(self):
        recs = self._bonded() + _clean_round(3) + [
            _rec(10, "RoundLapsed", digest=D, round=1),
            _rec(10, "Finalized", digest=D, round=1, verdict=3, repPool=0, atnPool=0),
        ]
        snap = tool_vetting_snapshot_from_logs(recs)
        assert snap["tools"][DIGEST] == {"round": 0, "auditors": {AUD_A: 30}}

    def test_order_independent(self):
        recs = self._bonded() + _clean_round(3)
        for i, r in enumerate(recs):
            r["index"] = i
        want = tool_vetting_snapshot_from_logs(recs)
        rng = random.Random(7)
        for _ in range(5):
            shuffled = recs[:]
            rng.shuffle(shuffled)
            assert tool_vetting_snapshot_from_logs(shuffled) == want


# ---------------------------------------------------------------------------
# 3. Reviewed composition split
# ---------------------------------------------------------------------------


def _own(seq: int, caller: str, own: float, digest: str = DIGEST):
    ev = _receipt_event(seq, caller, digest=digest)
    ev["own_share"] = own
    return ev


class TestReviewedRootShare:
    REGS = {
        DEP: {"trust_class": "pinned", "author": "dep-author"},
        DIGEST: {"trust_class": "pinned", "author": AUTHOR, "deps": DEP},
    }

    def _world(self):
        return _world(
            _registration_event(1, digest=DEP, author="dep-author", axis=2),
            _registration_event(2, digest=DIGEST, deps=DEP, axis=4),
        )

    def test_no_reviews_keeps_default(self):
        out = compute_tool_mint(self._world(), [_receipt_event(1, "c1")],
                                registrations=self.REGS)
        pd = out["per_digest"]
        ln = math.log1p(1)
        assert pd[DIGEST]["usage_term"] == pytest.approx(ln * COMPOSITE_ROOT_SHARE)
        assert pd[DEP]["usage_term"] == pytest.approx(ln * (1 - COMPOSITE_ROOT_SHARE))
        assert "own_share" not in out["positions_next"][DIGEST]

    def test_reviews_set_root_share_this_close(self):
        out = compute_tool_mint(self._world(), [_own(1, "c1", 0.2)],
                                registrations=self.REGS)
        ln = math.log1p(1)
        assert out["per_digest"][DIGEST]["usage_term"] == pytest.approx(ln * 0.2)
        assert out["per_digest"][DEP]["usage_term"] == pytest.approx(ln * 0.8)
        own = out["positions_next"][DIGEST]["own_share"]
        assert own["sum"] / own["mass"] == pytest.approx(0.2)

    def test_rep_weighted_mean_ignores_zero_rep_and_author(self):
        events = [_own(1, "rich", 1.0), _own(2, "poor", 0.0),
                  _own(3, "zero", 0.0), _own(4, AUTHOR, 0.0)]
        out = compute_tool_mint(
            self._world(), events, registrations=self.REGS,
            rep_shares={"rich": 0.3, "poor": 0.1, AUTHOR: 0.5},
        )
        own = out["positions_next"][DIGEST]["own_share"]
        assert own["sum"] / own["mass"] == pytest.approx(0.75)

    def test_carried_estimate_applies_next_close(self):
        first = compute_tool_mint(self._world(), [_own(1, "c1", 0.4)],
                                  registrations=self.REGS)
        second = compute_tool_mint(
            self._world(), [_receipt_event(5, "c2")], registrations=self.REGS,
            positions=first["positions_next"],
        )
        ln = math.log1p(1)
        assert second["per_digest"][DIGEST]["usage_term"] == pytest.approx(ln * 0.4)
        assert second["positions_next"][DIGEST]["own_share"] == \
            first["positions_next"][DIGEST]["own_share"]

    def test_padding_still_cannot_amplify(self):
        """own_share = 0 hands everything to deps, but the damped total is
        still conserved (damp-then-split)."""
        out = compute_tool_mint(self._world(), [_own(1, "c1", 0.0)],
                                registrations=self.REGS)
        total = sum(e["usage_term"] for e in out["per_digest"].values())
        assert total == pytest.approx(math.log1p(1))

    def test_usage_aggregation_clamps_and_drops_garbage(self):
        events = [_own(1, "c1", 1.7), _own(2, "c1", -3), _own(3, "c2", float("nan"))]
        events.append({**_receipt_event(4, "c3"), "own_share": "x"})
        usage = tool_usage_from_events(events)[DIGEST]["own_share_by_caller"]
        assert usage == {"c1": {"sum": 1.0, "n": 2}}

    def test_tool_used_event_serializes_own_share_only_when_set(self):
        assert "own_share" not in ToolUsed().to_dict()
        assert ToolUsed(own_share=0.25).to_dict()["own_share"] == 0.25


# ---------------------------------------------------------------------------
# 4. Successor merit carry
# ---------------------------------------------------------------------------


class TestSuccessorCarry:
    PRED_POS = {DIGEST: {"head": [0.0, 0.0, 0.0, 0.0, 0.5, -0.25],
                         "mass": [1.0, 1.0, 1.0, 1.0, 4.0, 3.0],
                         "own_share": {"sum": 0.6, "mass": 2.0}}}
    PRED_BOOK = {DIGEST: {"rev": {"correctness": {"sum": 1.5, "n": 3}}}}

    def _run(self, succ_author: str):
        regs = {
            DIGEST: {"trust_class": "pinned", "author": AUTHOR},
            SUCC: {"trust_class": "pinned", "author": succ_author,
                   "version_of": DIGEST},
        }
        return compute_tool_mint(
            _world(), [], registrations=regs,
            positions=copy.deepcopy(self.PRED_POS),
            tool_review_book=copy.deepcopy(self.PRED_BOOK),
        )

    def test_same_author_inherits_review_state(self):
        out = self._run(AUTHOR)
        succ = out["positions_next"][SUCC]
        assert succ["head"] == self.PRED_POS[DIGEST]["head"]
        assert succ["mass"] == self.PRED_POS[DIGEST]["mass"]
        assert succ["own_share"] == self.PRED_POS[DIGEST]["own_share"]
        assert out["tool_review_book_next"][SUCC] == self.PRED_BOOK[DIGEST]
        # The predecessor keeps its own state too.
        assert out["positions_next"][DIGEST]["head"] == self.PRED_POS[DIGEST]["head"]

    def test_other_author_inherits_nothing(self):
        out = self._run("someone-else")
        succ = out["positions_next"][SUCC]
        assert succ["head"] == [0.0] * 6
        assert succ["mass"] == [1.0] * 6
        assert "own_share" not in succ
        assert SUCC not in out["tool_review_book_next"]

    def test_inherits_only_at_first_appearance(self):
        regs = {
            DIGEST: {"trust_class": "pinned", "author": AUTHOR},
            SUCC: {"trust_class": "pinned", "author": AUTHOR, "version_of": DIGEST},
        }
        own_pos = {"head": [0.1] * 6, "mass": [2.0] * 6}
        out = compute_tool_mint(
            _world(), [], registrations=regs,
            positions={**copy.deepcopy(self.PRED_POS), SUCC: dict(own_pos)},
        )
        assert out["positions_next"][SUCC]["head"] == own_pos["head"]


# ---------------------------------------------------------------------------
# 5. Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_shuffled_inputs_identical_output(self):
        regs = {
            DEP: {"trust_class": "pinned", "author": "dep-author"},
            DIGEST: {"trust_class": "pinned", "author": AUTHOR, "deps": DEP},
            SUCC: {"trust_class": "pinned", "author": AUTHOR, "version_of": DIGEST},
        }
        events = [_own(1, "c1", 0.3), _own(2, "c2", 0.9), _receipt_event(3, "c3"),
                  _own(4, "c1", 0.5, digest=SUCC), _receipt_event(5, "c4", digest=DEP)]
        snap = {"auditor_share_bps": 2500, "tools": {
            DIGEST: {"round": 1, "auditors": {AUD_A: 7, AUD_B: 3}},
            DEP: {"round": 0, "auditors": {AUD_B: 1}},
        }}
        positions = {DIGEST: {"head": [0.0] * 6, "mass": [1.0] * 6,
                              "own_share": {"sum": 0.1, "mass": 0.5}}}
        rep = {"c1": 0.2, "c2": 0.1, "c3": 0.05, "c4": 0.3}

        def run(evs, snapshot, pos, shares):
            world = _world(
                _registration_event(1, digest=DEP, author="dep-author", axis=2),
                _registration_event(2, digest=DIGEST, deps=DEP, axis=4),
            )
            return compute_tool_mint(world, evs, registrations=regs,
                                     vetting_snapshot=snapshot, positions=pos,
                                     rep_shares=shares)

        want = run(events, snap, positions, rep)
        rng = random.Random(11)
        for _ in range(4):
            evs = events[:]
            rng.shuffle(evs)
            snap2 = {"tools": dict(reversed(list(snap["tools"].items()))),
                     "auditor_share_bps": 2500}
            snap2["tools"] = {d: {"auditors": dict(reversed(list(e["auditors"].items()))),
                                  "round": e["round"]}
                              for d, e in snap2["tools"].items()}
            rep2 = dict(reversed(list(rep.items())))
            got = run(evs, snap2, copy.deepcopy(positions), rep2)
            assert got == want

    def test_close_bit_identical_across_batch_order(self):
        kp_a, kp_c = Keypair.generate(), Keypair.generate()
        batches = _batches([_registration_event(1)], kp_a) + _batches(
            [_own(1, "c1", 0.4), _receipt_event(2, "c2")], kp_c)
        snap = _snapshot({AUD_A: 5, AUD_B: 2})
        r1 = federated_epoch_close(canonical_order(batches), emission_pool=3.0,
                                   vetting_snapshot=snap)
        shuffled = batches[:]
        random.Random(3).shuffle(shuffled)
        r2 = federated_epoch_close(canonical_order(shuffled), emission_pool=3.0,
                                   vetting_snapshot=copy.deepcopy(snap))
        assert r1["authoritative_payload"] == r2["authoritative_payload"]
        assert r1["tool_positions"] == r2["tool_positions"]


# ---------------------------------------------------------------------------
# 6. Plumbing: attest_tools gossips own_share; ABI matches ToolBond
# ---------------------------------------------------------------------------


async def test_attest_tools_gossips_own_share(tmp_path):
    from atn.agent_tools import execute_tool
    from tests.atn.test_tool_store import (
        _fake_embedder,
        _family,
        _make_runtime,
        _register_echo,
    )
    rt = _make_runtime(tmp_path)
    await _family(rt)
    await _register_echo(rt)
    rt.tool_store._embedder = _fake_embedder
    sunk: list = []
    rt.tool_store.event_sink = sunk.append
    out = await execute_tool(
        "attest_tools",
        {"judgments": [{"tool": "echo_tool", "ok": True, "own_share": 1.4},
                       {"tool": "echo_tool", "ok": True, "own_share": "junk"}],
         "context": "composing tools"},
        rt, caller_id="parent",
    )
    assert out["attested"] == 2
    assert sunk[0]["own_share"] == 1.0
    assert "own_share" not in sunk[1]


def test_tool_bond_event_abi_matches_contract():
    import json
    from pathlib import Path

    from nodes.common.voice_state import _TOOL_BOND_EVENTS_ABI

    art = Path("C:/code/dao/trustless-contracts/artifacts/contracts/"
               "ToolBond.sol/ToolBond.json")
    if not art.exists():
        pytest.skip("trustless-contracts ToolBond artifact not built")
    events = {e["name"]: e for e in json.loads(art.read_text())["abi"]
              if e["type"] == "event"}

    def shape(e):
        return [(i["type"], bool(i["indexed"])) for i in e["inputs"]]

    for spec in _TOOL_BOND_EVENTS_ABI:
        assert shape(spec) == shape(events[spec["name"]]), spec["name"]
