"""Phase 10.3b/c: chain submission driver glue tests.

The pieces this driver depends on (EpochAnchorer, AuthoritativeChainSubmitter)
have their own end-to-end eth_tester coverage in test_phase5_5_anchor.py
and test_phase5_6_authoritative_submission.py. This file tests the
glue layer only:

  - Winner daemons call anchor; non-winners skip it.
  - Per-agent submission walks the agent list and constructs one
    submitter per address (cached).
  - Blob from a successful anchor is written to the resolver so
    per-agent submitters can read it.
  - Disabled mode (no substrate_address / no key) is a no-op,
    not a crash.
"""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock, patch

from nodes.common.blob_resolver import InMemoryBlobResolver
from nodes.common.chain_submission_driver import (
    AgentChainIdentity,
    ChainSubmissionConfig,
    ChainSubmissionDriver,
)
from nodes.common.anchor_auth import AnchorFields
from nodes.common.epoch_anchorer import AnchorResult, PreparedAnchor
from nodes.common.federated_close_driver import FederatedCloseResult


def _fed(epoch_id: str, is_winner: bool, payload=None) -> FederatedCloseResult:
    close_result = {
        "epoch_id": epoch_id,
        "authoritative_payload": payload or {
            "agent_mint": {"0xAAA": 1.0},
            "epoch_root": "abc123",
        },
        "agent_mint": {"0xAAA": 1.0},
    }
    return FederatedCloseResult(
        epoch_id=epoch_id,
        close_result=close_result,
        senders=[b"x" * 32, b"y" * 32],
        winner=b"x" * 32,
        is_winner=is_winner,
        n_batches=2,
    )


def _make_driver(
    *,
    enabled: bool = True,
    agents: List[AgentChainIdentity] = None,
    gossip=None,
    timeout: float = 0.2,
):
    """Build a ChainSubmissionDriver with a mocked anchorer so we
    don't touch a real chain."""
    cfg = ChainSubmissionConfig(
        substrate_address="0x" + "1" * 40 if enabled else "",
        rpc_url="http://localhost:8545",
        chain_id=31337,
        daemon_private_key="0x" + "2" * 64 if enabled else "",
    )
    anchorer = _mock_anchorer() if enabled else None
    return ChainSubmissionDriver(
        config=cfg,
        agent_chain_resolver=lambda: list(agents or []),
        blob_resolver=InMemoryBlobResolver(),
        anchorer=anchorer,
        gossip=gossip,
        signature_timeout=timeout,
        poll_interval=0.05,
    )


DIGEST = bytes.fromhex("11" * 32)


def _mock_anchorer(quorum=None, anchor_count=None):
    """Mock anchorer with a fixed digest. ``quorum(signers) -> bool``
    decides the anchorQuorum view (default: always reached)."""
    anchorer = MagicMock()
    anchorer.is_anchored.return_value = False
    if anchor_count is not None:
        anchorer.anchor_count.return_value = anchor_count

    def _prepare(close_result):
        fields = AnchorFields("e1", b"\x01" * 32, b"\x00" * 32, b"\x00" * 32,
                              "cid_xyz", b"\x02" * 32, b"\x03" * 32)
        return PreparedAnchor(fields=fields, digest=DIGEST, result=AnchorResult(
            success=False, epoch_id="e1", agent_mint_cid="cid_xyz",
            agent_mint_blob=b"blob-bytes-here"))

    anchorer.prepare_anchor.side_effect = _prepare
    anchorer.are_registered.side_effect = lambda addrs: [True] * len(addrs)
    decide = quorum or (lambda signers: True)
    anchorer.anchor_quorum.side_effect = lambda signers: {
        "weight": len(signers), "total": 2, "genesis": True,
        "reached": decide(signers)}

    def _submit(prepared, signatures, **kw):
        r = prepared.result
        r.success = True
        r.tx_hash = "0xtx"
        r.signers = list(signatures)
        return r

    anchorer.submit_prepared.side_effect = _submit
    return anchorer


def test_disabled_mode_is_no_op():
    driver = _make_driver(enabled=False)
    assert driver.enabled is False
    out = driver.handle_federated_close(_fed("e1", is_winner=True))
    assert out["anchored"] is False
    assert out["agent_submissions"] == []


def test_winner_anchors_and_writes_blob():
    driver = _make_driver()
    out = driver.handle_federated_close(_fed("e1", is_winner=True))
    assert out["anchored"] is True
    assert out["anchor_tx"] == "0xtx"
    # Blob from anchor result should now be in the resolver.
    assert driver.blob_resolver.has("cid_xyz") or len(driver.blob_resolver) > 0


def test_non_winner_skips_anchor():
    driver = _make_driver()
    # Make sure anchorer is NOT called when not the winner.
    out = driver.handle_federated_close(_fed("e1", is_winner=False))
    assert out["anchored"] is False
    driver._anchorer.submit_prepared.assert_not_called()


def test_per_agent_submitter_cache():
    """Two close events => same submitter instance reused per agent
    address."""
    agents = [
        AgentChainIdentity(address="0xAAA", private_key="0x" + "a" * 64),
        AgentChainIdentity(address="0xBBB", private_key="0x" + "b" * 64),
    ]
    driver = _make_driver(agents=agents)

    # Mock submit_for_epoch on each cached submitter so we don't
    # hit a real chain.
    fake_result = MagicMock()
    fake_result.success = True
    fake_result.tx_hash = "0xagent_tx"
    fake_result.raw_mint = 1.0
    fake_result.error = ""

    with patch(
        "nodes.common.chain_submission_driver.AuthoritativeChainSubmitter.submit_for_epoch",
        return_value=fake_result,
    ):
        out1 = driver.handle_federated_close(_fed("e1", is_winner=True))
        out2 = driver.handle_federated_close(_fed("e2", is_winner=False))

    assert len(out1["agent_submissions"]) == 2
    assert len(out2["agent_submissions"]) == 2
    # Both calls returned for both agents.
    addrs = {r["agent_address"] for r in out1["agent_submissions"]}
    assert addrs == {"0xAAA", "0xBBB"}

    # Cache should hold one submitter per agent address.
    assert set(driver._submitter_cache.keys()) == {"0xAAA", "0xBBB"}


def test_agent_resolver_filtering():
    """Agents missing address or private_key are silently skipped."""
    agents = [
        AgentChainIdentity(address="0xAAA", private_key="0x" + "a" * 64),
        AgentChainIdentity(address="", private_key="0x" + "b" * 64),  # no addr
        AgentChainIdentity(address="0xCCC", private_key=""),           # no key
    ]
    driver = _make_driver(agents=agents)

    fake_result = MagicMock()
    fake_result.success = True
    fake_result.tx_hash = "0xagent_tx"
    fake_result.raw_mint = 0.5
    fake_result.error = ""

    with patch(
        "nodes.common.chain_submission_driver.AuthoritativeChainSubmitter.submit_for_epoch",
        return_value=fake_result,
    ):
        out = driver.handle_federated_close(_fed("e1", is_winner=True))

    assert len(out["agent_submissions"]) == 1
    assert out["agent_submissions"][0]["agent_address"] == "0xAAA"


# ---------------------------------------------------------------------------
# Anchor signing round (2026-10-12)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from eth_account import Account

from nodes.common.event_gossip import InMemoryHub, InMemoryTransport


def _ident(seed: str) -> AgentChainIdentity:
    key = "0x" + seed * 64
    return AgentChainIdentity(address=Account.from_key(key).address,
                              private_key=key)


def _gossip(hub):
    return SimpleNamespace(transport=InMemoryTransport(hub), topic="t/events")


def test_signing_round_publishes_and_winner_collects_peer_signature():
    hub = InMemoryHub()
    a, b = _ident("a"), _ident("b")
    need = {a.address, b.address}
    winner = _make_driver(agents=[a], gossip=_gossip(hub), timeout=2.0)
    winner._anchorer = _mock_anchorer(quorum=lambda s: set(s) >= need)
    peer = _make_driver(agents=[b], gossip=_gossip(hub))

    # The peer closes first: its signature is cached by the winner.
    out_peer = peer.handle_federated_close(_fed("e1", is_winner=False))
    assert out_peer["signatures_published"] == 1
    assert len(winner.signatures_for(DIGEST.hex())) == 1

    out = winner.handle_federated_close(_fed("e1", is_winner=True))
    assert out["anchored"] is True
    sigs = winner._anchorer.submit_prepared.call_args[0][1]
    assert len(sigs) == 2


def test_single_daemon_own_signatures_reach_quorum_without_waiting():
    a = _ident("c")
    driver = _make_driver(agents=[a], timeout=30.0)
    out = driver.handle_federated_close(_fed("e1", is_winner=True))
    assert out["anchored"] is True


def test_quorum_timeout_reports_error_and_does_not_submit():
    a = _ident("d")
    driver = _make_driver(agents=[a], timeout=0.2)
    driver._anchorer = _mock_anchorer(quorum=lambda s: False)
    out = driver.handle_federated_close(_fed("e1", is_winner=True))
    assert out["anchored"] is False
    assert "quorum not reached" in out["anchor_error"]
    driver._anchorer.submit_prepared.assert_not_called()


def test_stale_snapshot_is_never_signed():
    a = _ident("e")
    driver = _make_driver(agents=[a])
    driver._anchorer = _mock_anchorer(anchor_count=5)
    fed = _fed("e1", is_winner=True)
    fed.snapshot_anchor_count = 4
    out = driver.handle_federated_close(fed)
    assert "stale snapshot" in out["sign_skipped"]
    assert out["anchored"] is False
    driver._anchorer.prepare_anchor.assert_not_called()


def test_garbage_signature_messages_are_ignored():
    hub = InMemoryHub()
    driver = _make_driver(gossip=_gossip(hub))
    other = InMemoryTransport(hub)
    other.publish("t/events/anchor-sig/1", b"not json")
    other.publish("t/events/anchor-sig/1",
                  b'{"digest": "' + DIGEST.hex().encode() + b'", "sig": "00"}')
    assert driver.signatures_for(DIGEST.hex()) == []
