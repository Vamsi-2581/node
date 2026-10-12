"""Anchor authority: EIP-712 AnchorApproval digest, signing, recovery.

Mirror of ``Substrate.anchorDigest`` (2026-10-12). Every anchor must carry
signatures over this digest from registered, active agents whose combined
delegated REP votes (RepToken.getPastVotes at the previous anchor's
timestamp) strictly exceed ``anchorQuorumBps`` of REP past total supply;
while that supply is zero the deployer-fixed genesis signer set authorizes
instead (``genesisQuorum`` distinct signers). See docs/tool_substrate.md,
Decision (2026-10-12).

Digest (EIP-712, domain = name "AutonetSubstrate", version "1", chainId,
verifyingContract = the Substrate address):

    AnchorApproval(string epochId,bytes32 epochRoot,bytes32 prevEpochRoot,
                   bytes32 prevAnchorHash,string agentMintCid,
                   bytes32 payloadHash,bytes32 agentMintRoot)

Signatures are 65-byte (r, s, v) with v in {27, 28}; the contract requires
the RECOVERED signer addresses strictly ascending, so ``order_signatures``
sorts by recovered address and drops duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

DOMAIN_NAME = "AutonetSubstrate"
DOMAIN_VERSION = "1"
MAX_ANCHOR_SIGNERS = 64

_DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,"
         "address verifyingContract)")
ANCHOR_APPROVAL_TYPEHASH = keccak(
    text="AnchorApproval(string epochId,bytes32 epochRoot,bytes32 prevEpochRoot,"
         "bytes32 prevAnchorHash,string agentMintCid,bytes32 payloadHash,"
         "bytes32 agentMintRoot)")


@dataclass(frozen=True)
class AnchorFields:
    """The seven anchor fields every signer commits to."""
    epoch_id: str
    epoch_root: bytes
    prev_epoch_root: bytes
    prev_anchor_hash: bytes
    agent_mint_cid: str
    payload_hash: bytes
    agent_mint_root: bytes

    def as_args(self) -> Tuple:
        return (self.epoch_id, self.epoch_root, self.prev_epoch_root,
                self.prev_anchor_hash, self.agent_mint_cid,
                self.payload_hash, self.agent_mint_root)


def domain_separator(chain_id: int, verifying_contract: str) -> bytes:
    return keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [_DOMAIN_TYPEHASH, keccak(text=DOMAIN_NAME),
         keccak(text=DOMAIN_VERSION), int(chain_id),
         to_checksum_address(verifying_contract)],
    ))


def anchor_digest(fields: AnchorFields, chain_id: int,
                  verifying_contract: str) -> bytes:
    struct_hash = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "bytes32", "bytes32", "bytes32",
         "bytes32", "bytes32"],
        [ANCHOR_APPROVAL_TYPEHASH,
         keccak(text=fields.epoch_id),
         bytes(fields.epoch_root),
         bytes(fields.prev_epoch_root),
         bytes(fields.prev_anchor_hash),
         keccak(text=fields.agent_mint_cid),
         bytes(fields.payload_hash),
         bytes(fields.agent_mint_root)],
    ))
    return keccak(b"\x19\x01" + domain_separator(chain_id, verifying_contract)
                  + struct_hash)


def sign_digest(private_key: str, digest: bytes) -> bytes:
    from eth_account import Account
    pk = private_key if private_key.startswith("0x") else "0x" + private_key
    signer = getattr(Account, "unsafe_sign_hash", None)
    if signer is None:  # eth-account < 0.13
        signer = Account.signHash  # type: ignore[attr-defined]
    return bytes(signer(digest, pk).signature)


def recover_signer(digest: bytes, signature: bytes) -> str:
    from eth_account import Account
    return to_checksum_address(
        Account._recover_hash(digest, signature=bytes(signature)))


def order_signatures(digest: bytes,
                     signatures: Iterable[bytes]) -> Tuple[List[str], List[bytes]]:
    """Recover, dedupe by signer, sort ascending (the contract's order).

    Unrecoverable signatures are dropped. Returns (signers, signatures)
    aligned, capped at MAX_ANCHOR_SIGNERS (callers should pass the
    highest-weight subset first if they may exceed the cap).
    """
    by_signer: Dict[str, bytes] = {}
    for sig in signatures:
        try:
            addr = recover_signer(digest, sig)
        except Exception:
            continue
        by_signer.setdefault(addr, bytes(sig))
    ordered = sorted(by_signer.items(), key=lambda kv: int(kv[0], 16))
    ordered = ordered[:MAX_ANCHOR_SIGNERS]
    return [a for a, _ in ordered], [s for _, s in ordered]
