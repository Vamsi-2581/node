"""Anchor authority + on-chain conservation, daemon side (2026-10-12).

  - Python AnchorApproval digest == Substrate.anchorDigest (byte parity).
  - An outsider-signed anchor is rejected through the real EpochAnchorer;
    the genesis set's signature is accepted.
  - floor_mint_to_pool: integer shares never exceed the pool, and the
    merkle leaves built from them sum to <= the on-chain pool exactly.
  - An honest anchor at the seeded pool lets every leaf claim; the
    claims sum to <= mintPool.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

eth_tester = pytest.importorskip("eth_tester")

from eth_account import Account
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

from nodes.common.anchor_auth import AnchorFields, anchor_digest
from nodes.common.authoritative_submitter import epoch_id_hash
from nodes.common.epoch_anchorer import EpochAnchorer, EpochAnchorerConfig
from nodes.common.federated_reconcile import floor_mint_to_pool
from nodes.common.mint_merkle import mint_leaves, mint_merkle_proof
from tests.substrate_chain_helpers import (
    deploy_substrate,
    genesis_keys,
    seed_pool,
)

ARTIFACT = (Path(__file__).resolve().parent.parent / "artifacts" / "contracts"
            / "core" / "Substrate.sol" / "Substrate.json")


# ---------------------------------------------------------------------------
# Pure: integer flooring to the pool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(20))
def test_floor_mint_to_pool_never_exceeds_pool(seed):
    rng = random.Random(seed)
    pool_raw = rng.randint(1, 10 ** 13)
    pool = pool_raw / 1_000_000
    n = rng.randint(1, 40)
    weights = [rng.random() for _ in range(n)]
    total = sum(weights)
    addrs = [Account.create().address for _ in range(n)]
    # Pool-normalized floats, as apply_emission_pool produces them.
    mint = {a: pool * w / total for a, w in zip(addrs, weights)}
    floored = floor_mint_to_pool(mint, pool)
    leaves = mint_leaves(floored)
    s = sum(amt for _, amt in leaves)
    assert s <= pool_raw
    # Dust is bounded by one base unit per agent.
    assert pool_raw - s <= n


def test_floor_mint_to_pool_trims_float_overshoot():
    # Values that sum above the pool (an ulp-level overshoot, exaggerated).
    pool = 3.0
    mint = {"0x" + "1" * 40: 1.5000004, "0x" + "2" * 40: 1.5000004}
    floored = floor_mint_to_pool(mint, pool)
    assert sum(amt for _, amt in mint_leaves(floored)) == 3_000_000


# ---------------------------------------------------------------------------
# On-chain
# ---------------------------------------------------------------------------


@pytest.fixture
def chain():
    if not ARTIFACT.exists():
        pytest.skip(f"missing artifact: {ARTIFACT}")
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    w3 = Web3(EthereumTesterProvider())
    deployer = w3.eth.accounts[0]
    addr = deploy_substrate(w3, data["abi"], data["bytecode"], deployer)
    contract = w3.eth.contract(address=addr, abi=data["abi"])
    agents = w3.eth.accounts[1:4]
    for i, a in enumerate(agents):
        tx = contract.functions.registerAgent(
            Web3.keccak(text=f"aa-{i}"), f"peer-{i}".encode(),
        ).transact({"from": a, "gas": 500_000})
        assert w3.eth.wait_for_transaction_receipt(tx).status == 1
    anchorer = EpochAnchorer(
        config=EpochAnchorerConfig(epoch_anchor_address=addr),
        web3=w3, contract_abi=data["abi"],
    )
    return {"w3": w3, "contract": contract, "deployer": deployer,
            "agents": list(agents), "anchorer": anchorer}


def _close(mint, epoch_id):
    payload = {
        "schema": 3, "epoch_root": "22" * 32, "agent_mint": mint,
        "agent_novelty": {}, "total_mint": sum(mint.values()),
        "total_novelty": 0.0, "output_decimals": 10, "gate_applied": False,
        "n_batches": 1, "n_events": len(mint),
    }
    return {"authoritative_payload": payload, "agent_mint": mint,
            "epoch_id": epoch_id}


def test_digest_parity_with_contract(chain):
    w3, c = chain["w3"], chain["contract"]
    f = AnchorFields("e-parity", b"\x01" * 32, b"\x02" * 32, b"\x03" * 32,
                     "cid-parity", b"\x04" * 32, b"\x05" * 32)
    ours = anchor_digest(f, w3.eth.chain_id, c.address)
    theirs = bytes(c.functions.anchorDigest(*f.as_args()).call())
    assert ours == theirs


def test_outsider_anchor_rejected_genesis_accepted(chain):
    w3, anchorer = chain["w3"], chain["anchorer"]
    attacker = chain["agents"][0]
    from tests.substrate_chain_helpers import account_key
    evil = _close({attacker: 10 ** 9}, "evil")
    r = anchorer.anchor_close_result(
        evil, from_address=attacker,
        signer_keys=[account_key(w3, attacker)])
    assert not r.success
    r = anchorer.anchor_close_result(evil, from_address=attacker)
    assert not r.success
    assert chain["contract"].functions.anchorCount().call() == 0
    ok = anchorer.anchor_close_result(
        _close({attacker: 0.0}, "honest"), from_address=attacker,
        signer_keys=genesis_keys(w3))
    assert ok.success, ok.error
    assert [a.lower() for a in ok.signers] == [
        a.lower() for a in [w3.eth.accounts[9]]]


def test_pool_floored_close_claims_fully_and_conserves(chain):
    w3, c, anchorer = chain["w3"], chain["contract"], chain["anchorer"]
    pool_raw = seed_pool(w3, c, gross=10 ** 9 + 7)
    pool = pool_raw / 1_000_000
    agents = chain["agents"]
    w = [0.37, 1.11, 2.9]
    mint = {a: pool * x / sum(w) for a, x in zip(agents, w)}
    mint = floor_mint_to_pool(mint, pool)
    r = anchorer.anchor_close_result(
        _close(mint, "e-cons"), from_address=chain["deployer"],
        signer_keys=genesis_keys(w3))
    assert r.success, r.error
    anchor = c.functions.getAnchorByEpochId("e-cons").call()
    assert anchor[11] == pool_raw          # mintPool
    eid = epoch_id_hash("e-cons")
    total = 0
    for addr, amount in mint_leaves(mint):
        proof = mint_merkle_proof(mint, addr)
        tx = c.functions.recordTrainingForEpoch(amount, eid, proof).transact(
            {"from": addr, "gas": 1_500_000})
        assert w3.eth.wait_for_transaction_receipt(tx).status == 1
        total += amount
    assert total <= pool_raw
    assert c.functions.getAnchorByEpochId("e-cons").call()[12] == total
