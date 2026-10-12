"""Agent-side anchor submitter for the substrate-native chain surface.

Runs inside a daemon process but signs from an agent's keypair —
the on-chain submitter address is always an agent's, not a daemon's.

Phase 5.5 of native world-model integration. Updated in 5.6a to
target the consolidated ``Substrate.sol`` contract (which absorbed
the standalone EpochAnchor.sol after the pre-substrate contract
nuke).

After a federated epoch close, one agent (acting as the rotating
canonical-ordering coordinator for that epoch) submits the
authoritative payload to ``Substrate.submitAnchor`` from their own
keypair. Other agents in the federation verify the on-chain record
matches their local computation; mismatches indicate a fork and
trigger a dispute (later phase). Daemons are the wire transport,
not the consensus actor — the on-chain submitter is always an agent
address.

What this module does
---------------------

  - Reads the on-chain ``latestAnchorHash`` and ``latestEpochRoot``
    so the new anchor links into the chain correctly.
  - Encodes the authoritative payload via the canonical encoding spec.
  - Encodes the agent_mint blob, computes its CID.
  - Submits ``submitAnchor(...)`` to the EpochAnchor contract.
  - Returns an AnchorResult with the on-chain tx hash, the encoded
    payload bytes (so the caller can serve the blob over P2P), and
    the CID.

What this module does NOT do
----------------------------

  - Serve the off-chain blob — that's blob_store + libp2p in another
    layer.
  - Resolve forks. If the anchor submission reverts because another
    agent already submitted for this epoch, the caller treats that
    as "I'm second; verify their anchor matches mine and accept it,
    else dispute". Phase 5+ wires that.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .authoritative_encoding import (
    cid_for_blob,
    encode_agent_mint_blob,
    encode_authoritative_payload,
    payload_hash,
)
from .anchor_auth import (
    AnchorFields,
    anchor_digest,
    order_signatures,
    sign_digest,
)
from .mint_merkle import mint_merkle_root


logger = logging.getLogger(__name__)


@dataclass
class EpochAnchorerConfig:
    """Address of the deployed Substrate contract.

    Field name kept as ``epoch_anchor_address`` for callsite stability
    even though the chain surface is now the consolidated
    Substrate.sol (which absorbed EpochAnchor's responsibilities)."""
    epoch_anchor_address: str
    rpc_url: str = "http://localhost:8545"
    chain_id: int = 31337
    private_key: str = ""


@dataclass
class AnchorResult:
    success: bool
    tx_hash: str = ""
    epoch_id: str = ""
    epoch_root_hex: str = ""
    prev_epoch_root_hex: str = ""
    agent_mint_cid: str = ""
    agent_mint_blob: bytes = b""
    payload_bytes: bytes = b""
    payload_hash_hex: str = ""
    agent_mint_root_hex: str = ""
    error: str = ""
    # Anchor authority (2026-10-12): the EIP-712 digest signed and the
    # recovered signer set that accompanied the submission.
    digest_hex: str = ""
    signers: list = field(default_factory=list)


@dataclass
class PreparedAnchor:
    """Everything needed to sign + submit one anchor. ``result`` carries
    the encoded blobs (success=False until submitted)."""
    fields: AnchorFields
    digest: bytes
    result: AnchorResult


class EpochAnchorer:
    """Anchors federated epoch closes to ``Substrate.sol``.

    Thread-safe: a single anchorer instance can be called from the
    daemon's worker thread or an asyncio bridge.
    """

    def __init__(
        self,
        config: EpochAnchorerConfig,
        *,
        web3: Optional[Any] = None,
        contract_abi: Optional[list] = None,
    ):
        """Two construction modes.

        Production: pass ``config`` with rpc_url + private_key; the
        anchorer dials the RPC and loads the ABI from artifacts.

        Tests: pass ``web3`` + ``contract_abi`` directly so tests can
        run against an in-process EVM (eth_tester) without HTTP.
        """
        self.config = config
        self._lock = threading.RLock()
        self._w3 = web3
        self._abi = contract_abi
        self._contract = None
        self._submitter_address: Optional[str] = None

    def _get_contract(self):
        if self._contract is not None:
            return self._contract

        if self._w3 is not None and self._abi is not None:
            self._contract = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self.config.epoch_anchor_address),
                abi=self._abi,
            )
            return self._contract

        # Production path: build via blockchain.BlockchainInterface.
        from .blockchain import BlockchainInterface

        bc = BlockchainInterface(
            rpc_url=self.config.rpc_url,
            chain_id=self.config.chain_id,
            private_key=self.config.private_key,
        )
        from .contract_artifacts import load_abi
        self._abi = load_abi("Substrate")
        self._w3 = bc.web3
        self._contract = bc.web3.eth.contract(
            address=bc.web3.to_checksum_address(self.config.epoch_anchor_address),
            abi=self._abi,
        )
        return self._contract

    def _set_submitter_address(self, addr: str) -> None:
        """Override the submitter address (for tests where the caller
        has multiple accounts and wants to pick one)."""
        with self._lock:
            self._submitter_address = addr

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def latest_anchor_hash(self) -> bytes:
        contract = self._get_contract()
        h = contract.functions.latestAnchorHash().call()
        return bytes(h)

    def latest_epoch_root(self) -> bytes:
        contract = self._get_contract()
        r = contract.functions.latestEpochRoot().call()
        return bytes(r)

    def is_anchored(self, epoch_id: str) -> bool:
        contract = self._get_contract()
        return bool(contract.functions.isAnchored(epoch_id).call())

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def chain_id(self) -> int:
        """The chain id the EIP-712 domain binds to (read from the node,
        cached; falls back to config when the node cannot answer)."""
        cached = getattr(self, "_chain_id_cache", None)
        if cached is not None:
            return cached
        self._get_contract()
        try:
            cid = int(self._w3.eth.chain_id)
        except Exception:
            cid = int(self.config.chain_id)
        self._chain_id_cache = cid
        return cid

    def prepare_anchor(self, close_result: Dict[str, Any]) -> PreparedAnchor:
        """Encode a federated close result into anchor fields + the
        AnchorApproval digest every signer signs. Reads the chain head
        (latestEpochRoot / latestAnchorHash), so honest daemons reading
        the same head derive the identical digest. Raises ValueError on a
        malformed close result."""
        with self._lock:
            payload = close_result.get("authoritative_payload")
            if payload is None:
                raise ValueError("close_result has no authoritative_payload")

            epoch_id = str(close_result.get("epoch_id") or "")
            if not epoch_id:
                # Fall back to a deterministic id derived from the root.
                epoch_id = f"e_{payload.get('epoch_root', '')[:16]}"
            epoch_root_hex = str(payload.get("epoch_root", ""))
            if not epoch_root_hex:
                raise ValueError("authoritative_payload missing epoch_root")

            prev_root_bytes = self.latest_epoch_root()
            prev_anchor_bytes = self.latest_anchor_hash()
            prev_root_hex = prev_root_bytes.hex()

            # Encode the authoritative payload canonically.
            try:
                payload_bytes = encode_authoritative_payload(
                    payload,
                    epoch_id=epoch_id,
                    epoch_root_hex=epoch_root_hex,
                    prev_epoch_root_hex=prev_root_hex,
                )
            except Exception as e:
                raise ValueError(f"encoding failed: {e}") from e

            ph = payload_hash(payload_bytes)

            # Encode + CID the off-chain agent_mint blob (money only,
            # Decision 2026-07-10 — no agent_rep map; REP is DAO-claimed).
            agent_mint = close_result.get("agent_mint") or payload.get("agent_mint", {})
            mint_blob = encode_agent_mint_blob(agent_mint)
            cid = cid_for_blob(mint_blob)

            # Merkle root over the mint map: agents prove their share
            # against this when claiming (recordTrainingForEpoch), a
            # 2-field (agent, amount) leaf.
            mint_root = mint_merkle_root(agent_mint)

            fields = AnchorFields(
                epoch_id=epoch_id,
                epoch_root=bytes.fromhex(epoch_root_hex),
                prev_epoch_root=prev_root_bytes,
                prev_anchor_hash=prev_anchor_bytes,
                agent_mint_cid=cid,
                payload_hash=ph,
                agent_mint_root=mint_root,
            )
            digest = anchor_digest(
                fields, self.chain_id(), self.config.epoch_anchor_address)
            result = AnchorResult(
                success=False,
                epoch_id=epoch_id,
                epoch_root_hex=epoch_root_hex,
                prev_epoch_root_hex=prev_root_hex,
                agent_mint_cid=cid,
                agent_mint_blob=mint_blob,
                payload_bytes=payload_bytes,
                payload_hash_hex=ph.hex(),
                agent_mint_root_hex=mint_root.hex(),
                digest_hex=digest.hex(),
            )
            return PreparedAnchor(fields=fields, digest=digest, result=result)

    def anchor_count(self) -> int:
        return int(self._get_contract().functions.anchorCount().call())

    def are_registered(self, addrs: List[str]) -> List[bool]:
        if not addrs:
            return []
        contract = self._get_contract()
        return [bool(x) for x in contract.functions.areRegistered(
            [self._w3.to_checksum_address(a) for a in addrs]).call()]

    def anchor_quorum(self, signers: List[str]) -> Dict[str, Any]:
        """Contract view: quorum status of a (sorted) signer set for the
        NEXT anchor. {weight, total, genesis, reached}."""
        contract = self._get_contract()
        w, t, g, ok = contract.functions.anchorQuorum(
            [self._w3.to_checksum_address(a) for a in signers]).call()
        return {"weight": int(w), "total": int(t), "genesis": bool(g),
                "reached": bool(ok)}

    def submit_prepared(
        self,
        prepared: PreparedAnchor,
        signatures: List[bytes],
        *,
        from_address: Optional[str] = None,
    ) -> AnchorResult:
        """Submit a prepared anchor with the collected signatures (any
        order, duplicates fine: they are recovered, deduped and sorted)."""
        with self._lock:
            r = prepared.result
            signers, ordered = order_signatures(prepared.digest, signatures)
            r.signers = signers
            try:
                r.tx_hash = self._submit(
                    epoch_id=prepared.fields.epoch_id,
                    epoch_root=prepared.fields.epoch_root,
                    prev_epoch_root=prepared.fields.prev_epoch_root,
                    prev_anchor_hash=prepared.fields.prev_anchor_hash,
                    agent_mint_cid=prepared.fields.agent_mint_cid,
                    payload_hash_bytes=prepared.fields.payload_hash,
                    agent_mint_root=prepared.fields.agent_mint_root,
                    signatures=ordered,
                    from_address=from_address,
                )
                r.success = True
                r.error = ""
            except Exception as e:
                r.success = False
                r.error = f"submitAnchor reverted: {e}"
            return r

    def anchor_close_result(
        self,
        close_result: Dict[str, Any],
        *,
        from_address: Optional[str] = None,
        signatures: Optional[List[bytes]] = None,
        signer_keys: Optional[List[str]] = None,
    ) -> AnchorResult:
        """Prepare + sign + submit in one call.

        ``signatures`` are pre-collected AnchorApproval signatures;
        ``signer_keys`` are agent private keys this process signs with
        directly (single-daemon / tests). ``from_address`` overrides the
        relaying submitter address (useful in tests); the relayer itself
        carries no authority.
        """
        with self._lock:
            try:
                prepared = self.prepare_anchor(close_result)
            except ValueError as e:
                return AnchorResult(success=False, error=str(e))
            sigs = list(signatures or [])
            for k in signer_keys or []:
                sigs.append(sign_digest(k, prepared.digest))
            return self.submit_prepared(
                prepared, sigs, from_address=from_address)

    def _submit(
        self,
        *,
        epoch_id: str,
        epoch_root: bytes,
        prev_epoch_root: bytes,
        prev_anchor_hash: bytes,
        agent_mint_cid: str,
        payload_hash_bytes: bytes,
        agent_mint_root: bytes,
        signatures: Optional[List[bytes]] = None,
        from_address: Optional[str] = None,
    ) -> str:
        contract = self._get_contract()
        w3 = self._w3
        if w3 is None:
            raise RuntimeError("EpochAnchorer has no Web3 instance")
        sigs = [bytes(x) for x in (signatures or [])]

        # Resolve sender: explicit override > previously-set address >
        # derive from private key (production) > eth_accounts[0]
        # (eth_tester only, where the RPC actually exposes a managed
        # wallet).
        sender = from_address or self._submitter_address
        if sender is None and self.config.private_key:
            from eth_account import Account
            pk = self.config.private_key
            if not pk.startswith("0x"):
                pk = "0x" + pk
            sender = Account.from_key(pk).address
        if sender is None:
            # eth_tester fallback. eth_accounts is disabled on most
            # public RPCs (e.g. Etherlink shadownet), so we only
            # call it when nothing else worked.
            try:
                accts = w3.eth.accounts
            except Exception:
                accts = []
            sender = accts[0] if accts else None
        if sender is None:
            raise RuntimeError("no submitter address available")

        # Test path: account is unlocked in eth_tester, sign via transact.
        if not self.config.private_key:
            tx_hash = contract.functions.submitAnchor(
                epoch_id,
                epoch_root,
                prev_epoch_root,
                prev_anchor_hash,
                agent_mint_cid,
                payload_hash_bytes,
                agent_mint_root,
                sigs,
            ).transact({"from": sender})
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt.status != 1:
                raise RuntimeError(f"tx reverted: {receipt}")
            logger.info(
                "Anchor submitted: epoch=%s, tx=%s", epoch_id, tx_hash.hex(),
            )
            return tx_hash.hex()

        # Production path: sign with private key. Estimate gas with a
        # margin so we work on networks with higher actual costs (e.g.
        # Etherlink shadownet reports ~1.2M for submitAnchor). A flat
        # hardcode bites in real-world deployments.
        try:
            estimated = contract.functions.submitAnchor(
                epoch_id,
                epoch_root,
                prev_epoch_root,
                prev_anchor_hash,
                agent_mint_cid,
                payload_hash_bytes,
                agent_mint_root,
                sigs,
            ).estimate_gas({"from": sender})
            gas_limit = max(int(estimated * 12 // 10), 1_500_000)
        except Exception:
            gas_limit = 2_500_000
        tx = contract.functions.submitAnchor(
            epoch_id,
            epoch_root,
            prev_epoch_root,
            prev_anchor_hash,
            agent_mint_cid,
            payload_hash_bytes,
            agent_mint_root,
            sigs,
        ).build_transaction({
            "from": sender,
            "nonce": w3.eth.get_transaction_count(sender),
            "gas": gas_limit,
            "gasPrice": w3.eth.gas_price,
            "chainId": self.config.chain_id,
        })
        signed = w3.eth.account.sign_transaction(tx, self.config.private_key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status != 1:
            raise RuntimeError(f"tx reverted: {receipt}")
        logger.info("Anchor submitted: epoch=%s, tx=%s", epoch_id, tx_hash.hex())
        return tx_hash.hex()
