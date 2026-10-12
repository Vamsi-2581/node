"""Shared eth_tester helpers for Substrate (anchor authority, 2026-10-12).

Substrate now requires (a) EIP-712 AnchorApproval signatures from a quorum
on every submitAnchor, and (b) an on-chain fees-only pool covering every
recordTrainingForEpoch. These helpers give Python chain tests the same
setup the hardhat helpers (test/helpers.js) give the JS tests:

  - deploy with a REP-less genesis set (eth_tester account 9, registered),
    vaultMinter = account 8 (test faucet for purchased ATN);
  - ``seed_pool``: buy ATN, burn real service fees, anchor a seed epoch so
    the NEXT anchor's mintPool covers the test's mints (lag-1);
  - ``genesis_keys``: private keys to pass as ``signer_keys`` to
    ``EpochAnchorer.anchor_close_result`` or ``sign_anchor_fields``.

Accounts 6..9 are reserved here; tests use 0..5.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from web3 import Web3

from nodes.common.anchor_auth import (
    AnchorFields,
    anchor_digest,
    order_signatures,
    sign_digest,
)

ZERO = "0x0000000000000000000000000000000000000000"
SLOT_POOL_RECIPIENT = 6
SLOT_POOL_PAYER = 7
SLOT_VAULT = 8
SLOT_GENESIS = 9
DEFAULT_SEED_GROSS = 10 ** 12   # burns 1.25e10 base units (12,500 ATN)


def account_key(w3: Web3, address: str) -> str:
    """Private key (0x hex) of an eth_tester managed account."""
    backend = w3.provider.ethereum_tester.backend
    from eth_account import Account
    for k in backend.account_keys:
        kb = k.to_bytes() if hasattr(k, "to_bytes") else bytes(k)
        if Account.from_key(kb).address.lower() == address.lower():
            return "0x" + kb.hex()
    raise KeyError(address)


def genesis_address(w3: Web3) -> str:
    return w3.eth.accounts[SLOT_GENESIS]


def genesis_keys(w3: Web3) -> List[str]:
    return [account_key(w3, genesis_address(w3))]


def deploy_substrate(
    w3: Web3,
    abi: list,
    bytecode: str,
    deployer: str,
    *,
    treasury: Optional[str] = None,
    vault_minter: Optional[str] = None,
    governor: str = ZERO,
    rep_token: str = ZERO,
    quorum_bps: int = 5000,
    genesis: Optional[Sequence[str]] = None,
    genesis_quorum: Optional[int] = None,
    register_genesis: bool = True,
) -> str:
    accts = w3.eth.accounts
    treasury = deployer if treasury is None else treasury
    vault_minter = accts[SLOT_VAULT] if vault_minter is None else vault_minter
    genesis = list(genesis) if genesis is not None else [accts[SLOT_GENESIS]]
    if genesis_quorum is None:
        genesis_quorum = 1 if genesis else 0
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = factory.constructor(
        treasury, vault_minter, governor, rep_token, quorum_bps,
        genesis, genesis_quorum,
    ).transact({"from": deployer, "gas": 12_000_000})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    assert receipt.status == 1
    addr = receipt.contractAddress
    if register_genesis:
        c = w3.eth.contract(address=addr, abi=abi)
        for i, g in enumerate(genesis):
            tx = c.functions.registerAgent(
                Web3.keccak(text=f"genesis-signer-{i}"), b"genesis-peer",
            ).transact({"from": g, "gas": 400_000})
            assert w3.eth.wait_for_transaction_receipt(tx).status == 1
    return addr


def sign_anchor_fields(
    w3: Web3, contract, fields: AnchorFields, keys: Sequence[str],
) -> List[bytes]:
    digest = anchor_digest(fields, w3.eth.chain_id, contract.address)
    _, ordered = order_signatures(digest, [sign_digest(k, digest) for k in keys])
    return ordered


def submit_anchor_signed(
    w3: Web3,
    contract,
    sender: str,
    epoch_id: str,
    epoch_root: bytes,
    agent_mint_cid: str,
    payload_hash: bytes,
    agent_mint_root: bytes,
    *,
    keys: Optional[Sequence[str]] = None,
    gas: int = 3_000_000,
):
    """Chain onto the current head and submit, signed by ``keys``
    (default: the genesis set). Returns the receipt."""
    fields = AnchorFields(
        epoch_id=epoch_id,
        epoch_root=bytes(epoch_root),
        prev_epoch_root=bytes(contract.functions.latestEpochRoot().call()),
        prev_anchor_hash=bytes(contract.functions.latestAnchorHash().call()),
        agent_mint_cid=agent_mint_cid,
        payload_hash=bytes(payload_hash),
        agent_mint_root=bytes(agent_mint_root),
    )
    sigs = sign_anchor_fields(
        w3, contract, fields, keys if keys is not None else genesis_keys(w3))
    tx = contract.functions.submitAnchor(
        *fields.as_args(), sigs).transact({"from": sender, "gas": gas})
    return w3.eth.wait_for_transaction_receipt(tx)


_seed_counter = [0]


def seed_pool(w3: Web3, contract, gross: int = DEFAULT_SEED_GROSS) -> int:
    """Burn real fees (purchased ATN -> payForService) and anchor a seed
    epoch closing that window. Returns the pool the NEXT anchor carries."""
    accts = w3.eth.accounts
    payer, recipient = accts[SLOT_POOL_PAYER], accts[SLOT_POOL_RECIPIENT]
    tx = contract.functions.mintFromVault(payer, gross).transact(
        {"from": accts[SLOT_VAULT], "gas": 300_000})
    assert w3.eth.wait_for_transaction_receipt(tx).status == 1
    tx = contract.functions.payForService(
        recipient, gross, b"\x00" * 32).transact({"from": payer, "gas": 400_000})
    assert w3.eth.wait_for_transaction_receipt(tx).status == 1
    _seed_counter[0] += 1
    n = _seed_counter[0]
    r = submit_anchor_signed(
        w3, contract, payer, f"seed-{n}",
        Web3.keccak(text=f"seed-root-{n}"), f"seed-cid-{n}",
        Web3.keccak(text=f"seed-payload-{n}"), b"\x00" * 32)
    assert r.status == 1, r
    return int(contract.functions.nextMintPool().call())
