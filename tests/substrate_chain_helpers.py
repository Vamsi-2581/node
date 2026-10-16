"""Shared eth_tester helpers for Substrate (governed anchors, 2026-10-16).

``Substrate.anchor`` is governor-only (production: the DAO timelock). The
Python chain tests deploy with an EOA governor (the deployer, eth_tester
account 0, by default) and anchor directly from it. Every
``recordTrainingForEpoch`` must also be covered by the on-chain fees-only
pool, so:

  - ``deploy_substrate``: 3-arg constructor (treasury, vaultMinter,
    governor); vaultMinter = account 8 (test faucet for purchased ATN);
  - ``anchor_direct``: chain onto the current head and anchor from the
    governor;
  - ``seed_pool``: buy ATN, burn real service fees, anchor a seed epoch so
    the NEXT anchor's mintPool covers the test's mints (lag-1).

Accounts 6..8 are reserved here; tests use 0..5.
"""

from __future__ import annotations

from typing import Optional

from web3 import Web3

ZERO = "0x0000000000000000000000000000000000000000"
SLOT_POOL_RECIPIENT = 6
SLOT_POOL_PAYER = 7
SLOT_VAULT = 8
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


def deploy_substrate(
    w3: Web3,
    abi: list,
    bytecode: str,
    deployer: str,
    *,
    treasury: Optional[str] = None,
    vault_minter: Optional[str] = None,
    governor: Optional[str] = None,
) -> str:
    """Deploy Substrate. ``governor`` defaults to ``deployer``: the
    account the tests anchor from."""
    accts = w3.eth.accounts
    treasury = deployer if treasury is None else treasury
    vault_minter = accts[SLOT_VAULT] if vault_minter is None else vault_minter
    governor = deployer if governor is None else governor
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = factory.constructor(treasury, vault_minter, governor).transact(
        {"from": deployer, "gas": 12_000_000})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    assert receipt.status == 1
    return receipt.contractAddress


def anchor_direct(
    w3: Web3,
    contract,
    epoch_id: str,
    epoch_root: bytes,
    agent_mint_cid: str,
    payload_hash: bytes,
    agent_mint_root: bytes,
    *,
    sender: Optional[str] = None,
    gas: int = 3_000_000,
):
    """Chain onto the current head and anchor from ``sender`` (default:
    Substrate's governor). Returns the receipt."""
    sender = sender or contract.functions.governor().call()
    tx = contract.functions.anchor(
        epoch_id,
        bytes(epoch_root),
        bytes(contract.functions.latestEpochRoot().call()),
        bytes(contract.functions.latestAnchorHash().call()),
        agent_mint_cid,
        bytes(payload_hash),
        bytes(agent_mint_root),
    ).transact({"from": sender, "gas": gas})
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
    r = anchor_direct(
        w3, contract, f"seed-{n}",
        Web3.keccak(text=f"seed-root-{n}"), f"seed-cid-{n}",
        Web3.keccak(text=f"seed-payload-{n}"), b"\x00" * 32)
    assert r.status == 1, r
    return int(contract.functions.nextMintPool().call())
