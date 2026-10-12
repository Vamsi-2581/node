"""Chain submission driver — anchor + per-agent mint submission.

Triggered by ``FederatedCloseDriver`` results (Phase 10.3a). Two
responsibilities:

  - **Anchor**: when this daemon is the deterministic-random winner
    for an epoch, encode the close result and submit the anchor to
    Substrate.sol. Only one daemon submits per epoch; the contract's
    ``isAnchored(epoch_id)`` rejects duplicates as defense in depth.

  - **Per-agent mint**: for every locally-registered agent (each one
    holds its own private key + 0x address via the agent registry),
    construct an ``AuthoritativeChainSubmitter`` and call
    ``submit_for_epoch(epoch_id)``. The submitter reads the anchor
    on chain, fetches the blob, decodes the agent's share, and
    submits ``recordTrainingForEpoch``. Per-agent submission is
    inherently parallel — every agent does it independently.

  - **Anchor signing round** (2026-10-12): Substrate only accepts an
    anchor carrying EIP-712 AnchorApproval signatures from registered
    agents whose delegated REP votes (at the previous anchor's
    timestamp) strictly exceed the quorum share of REP supply (genesis:
    a deployer-fixed signer set). Every daemon prepares the identical
    anchor from the bit-identical close, signs the digest with each
    local agent key, and gossips the signatures on
    ``<event topic>/anchor-sig/1``. The rotating winner collects until
    ``Substrate.anchorQuorum`` reports the quorum reached (or
    ``signature_timeout`` elapses), then relays the anchor. A single
    daemon whose own agents carry the quorum submits immediately.

The anchor blob (off-chain) is written to a ``BlobResolver`` that
the per-agent submitters read from. For single-process testing an
``InMemoryBlobResolver`` is fine; cross-daemon needs a libp2p-backed
resolver (Phase 10.3d).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .authoritative_submitter import (
    AuthoritativeChainSubmitter,
    AuthoritativeSubmissionResult,
    AuthoritativeSubmitterConfig,
)
from .anchor_auth import order_signatures, recover_signer, sign_digest
from .blob_resolver import BlobResolver, InMemoryBlobResolver
from .epoch_anchorer import (
    AnchorResult,
    EpochAnchorer,
    EpochAnchorerConfig,
)
from .federated_close_driver import FederatedCloseResult


logger = logging.getLogger(__name__)

ANCHOR_SIG_TOPIC_SUFFIX = "/anchor-sig/1"
# Bounded signature cache: digests kept, signers per digest.
_MAX_DIGESTS = 16
_MAX_SIGNERS_PER_DIGEST = 512


@dataclass
class ChainSubmissionConfig:
    """Per-daemon chain config. Mirrors BlockchainConfig but renamed
    so the submitter doesn't import the broader autonet config."""
    substrate_address: str
    rpc_url: str = "http://localhost:8545"
    chain_id: int = 31337
    daemon_private_key: str = ""


# Resolver: local agent_id (YAML id) -> (address, private_key) or None.
# Used by the chain submission driver to find the per-agent signing
# key when constructing AuthoritativeChainSubmitters.
@dataclass
class AgentChainIdentity:
    address: str
    private_key: str


AgentChainResolver = Callable[[], List[AgentChainIdentity]]


class ChainSubmissionDriver:
    """Owns the anchor submitter and a cache of per-agent submitters.

    A single instance per daemon. Subscribes via
    ``handle_federated_close(fed_result)`` to ``FederatedCloseDriver``
    output (registered through
    ``AutonetService.add_federated_close_subscriber``).
    """

    def __init__(
        self,
        *,
        config: ChainSubmissionConfig,
        agent_chain_resolver: AgentChainResolver,
        blob_resolver: Optional[BlobResolver] = None,
        anchorer: Optional[EpochAnchorer] = None,
        gossip: Optional[Any] = None,
        signature_timeout: float = 60.0,
        poll_interval: float = 1.0,
    ):
        self.config = config
        self._agent_resolver = agent_chain_resolver
        # Use `is None` rather than `or` because BlobResolver impls
        # define ``__len__`` and an empty resolver evaluates falsy.
        self._blob_resolver = (
            blob_resolver if blob_resolver is not None else InMemoryBlobResolver()
        )
        self._lock = threading.RLock()
        # Cache: agent_address -> AuthoritativeChainSubmitter.
        self._submitter_cache: Dict[str, AuthoritativeChainSubmitter] = {}
        # Anchor side. Configurable so tests can pass an in-process
        # anchorer with eth_tester.
        self._anchorer = anchorer or self._build_anchorer()
        # Anchor signing round: digest hex -> {signer address: signature}.
        self.signature_timeout = float(signature_timeout)
        self.poll_interval = float(poll_interval)
        self._sig_cond = threading.Condition()
        self._sig_pool: "OrderedDict[str, Dict[str, bytes]]" = OrderedDict()
        self._sig_transport: Optional[Any] = None
        self._sig_topic: str = ""
        if gossip is not None:
            self.attach_gossip(gossip)

    # ------------------------------------------------------------------
    # Signature gossip
    # ------------------------------------------------------------------

    def attach_gossip(self, gossip: Any) -> None:
        """Subscribe to the anchor-signature topic next to the gossip's
        event topic (same transport, same peer set)."""
        transport = getattr(gossip, "transport", None)
        topic = getattr(gossip, "topic", "")
        if transport is None or not topic:
            return
        self._sig_transport = transport
        self._sig_topic = topic + ANCHOR_SIG_TOPIC_SUFFIX
        transport.subscribe(self._sig_topic, self._on_sig_message)

    def _store_sig(self, digest_hex: str, sig: bytes) -> Optional[str]:
        """Recover + store one signature. Returns the signer or None."""
        try:
            signer = recover_signer(bytes.fromhex(digest_hex), sig)
        except Exception:
            return None
        with self._sig_cond:
            bucket = self._sig_pool.get(digest_hex)
            if bucket is None:
                bucket = {}
                self._sig_pool[digest_hex] = bucket
                while len(self._sig_pool) > _MAX_DIGESTS:
                    self._sig_pool.popitem(last=False)
            if signer not in bucket and len(bucket) < _MAX_SIGNERS_PER_DIGEST:
                bucket[signer] = bytes(sig)
            self._sig_cond.notify_all()
        return signer

    def _on_sig_message(self, topic: str, data: bytes) -> None:
        if topic != self._sig_topic:
            return
        try:
            msg = json.loads(bytes(data).decode("utf-8"))
            digest_hex = str(msg["digest"]).lower().removeprefix("0x")
            sig = bytes.fromhex(str(msg["sig"]).removeprefix("0x"))
            if len(bytes.fromhex(digest_hex)) != 32 or len(sig) != 65:
                return
        except Exception:
            return
        self._store_sig(digest_hex, sig)

    def _publish_sig(self, epoch_id: str, digest_hex: str, sig: bytes) -> None:
        if self._sig_transport is None:
            return
        msg = json.dumps({
            "v": 1, "epoch_id": epoch_id, "digest": digest_hex,
            "sig": sig.hex(),
        }, sort_keys=True).encode("utf-8")
        try:
            self._sig_transport.publish(self._sig_topic, msg)
        except Exception as e:
            logger.warning("anchor signature publish failed: %s", e)

    def signatures_for(self, digest_hex: str) -> List[bytes]:
        with self._sig_cond:
            return list((self._sig_pool.get(digest_hex) or {}).values())

    def _build_anchorer(self) -> Optional[EpochAnchorer]:
        if not self.config.substrate_address:
            logger.info(
                "ChainSubmissionDriver: no substrate_address; "
                "chain submission disabled"
            )
            return None
        if not self.config.daemon_private_key:
            logger.info(
                "ChainSubmissionDriver: no daemon_private_key; "
                "chain submission disabled"
            )
            return None
        return EpochAnchorer(
            EpochAnchorerConfig(
                epoch_anchor_address=self.config.substrate_address,
                rpc_url=self.config.rpc_url,
                chain_id=self.config.chain_id,
                private_key=self.config.daemon_private_key,
            )
        )

    @property
    def blob_resolver(self) -> BlobResolver:
        return self._blob_resolver

    @property
    def enabled(self) -> bool:
        return self._anchorer is not None

    def handle_federated_close(self, fed: FederatedCloseResult) -> Dict[str, Any]:
        """Anchor (if winner) + submit per-agent mints. Subscriber
        method registered with AutonetService.

        Returns a diagnostic dict with what happened.
        """
        out: Dict[str, Any] = {
            "epoch_id": fed.epoch_id,
            "is_winner": fed.is_winner,
            "anchored": False,
            "anchor_tx": "",
            "agent_submissions": [],
        }

        if not self.enabled:
            return out

        # State sync: every daemon computed the same canonical-world
        # checkpoint blob — publish it so rejoining peers can fetch it
        # from anyone, not just the anchor winner.
        if fed.world_checkpoint_blob:
            try:
                self._blob_resolver.put(fed.world_checkpoint_blob)
                out["world_cid"] = fed.world_cid
            except Exception as e:
                logger.warning("world checkpoint blob put failed: %s", e)
        # Verifiable carry-over (payload schema 4): same contract as the
        # world blob. Every daemon computed the identical bundle, so every
        # daemon serves it; a joiner verifies it against the anchored
        # payload's carry_cid, so it doesn't matter which peer answers.
        carry_blob = getattr(fed, "carry_bundle_blob", b"")
        if carry_blob:
            try:
                self._blob_resolver.put(carry_blob)
                out["carry_cid"] = fed.carry_cid
            except Exception as e:
                logger.warning("carry-over bundle blob put failed: %s", e)

        # Anchor signing round (every daemon), then the winner relays.
        prepared = self._sign_round(fed, out)
        if fed.is_winner and prepared is not None:
            anchor_result = self._anchor(fed, prepared)
            out["anchored"] = anchor_result.success
            out["anchor_tx"] = anchor_result.tx_hash
            if not anchor_result.success:
                out["anchor_error"] = anchor_result.error

        # Per-agent submissions. Run regardless of is_winner — every
        # daemon submits each of its agents' mints; the anchor must
        # already be on chain (winner posted it just above on the
        # winner daemon, or another daemon raced ahead).
        agent_results = self._submit_agents(fed.epoch_id)
        out["agent_submissions"] = agent_results
        return out

    def _sign_round(self, fed: FederatedCloseResult,
                    out: Dict[str, Any]) -> Optional[Any]:
        """Prepare the anchor, sign its digest with every local agent key,
        gossip the signatures. Returns the PreparedAnchor, or None when
        this daemon must not sign (stale snapshot, already anchored,
        malformed close)."""
        anchorer = self._anchorer
        try:
            if anchorer.is_anchored(fed.epoch_id):
                out["sign_skipped"] = "already anchored"
                return None
            snap = getattr(fed, "snapshot_anchor_count", None)
            if snap is not None:
                count_fn = getattr(anchorer, "anchor_count", None)
                if callable(count_fn):
                    now = int(count_fn())
                    if now != int(snap):
                        # The close priced its pool at anchor #snap; the
                        # chain moved, so the on-chain mintPool for this
                        # anchor would differ. Never sign a stale close.
                        out["sign_skipped"] = (
                            f"stale snapshot (closed at anchorCount={snap}, "
                            f"chain has {now})")
                        logger.warning(
                            "anchor signing skipped for epoch %s: %s",
                            fed.epoch_id, out["sign_skipped"])
                        return None
            prepared = anchorer.prepare_anchor(fed.close_result)
        except Exception as e:
            logger.warning("anchor prepare failed for epoch %s: %s",
                           fed.epoch_id, e)
            out["sign_skipped"] = f"prepare failed: {e}"
            return None

        digest_hex = prepared.digest.hex()
        out["anchor_digest"] = digest_hex
        signed = 0
        try:
            agents = self._agent_resolver()
        except Exception as e:
            logger.warning("agent_chain_resolver failed: %s", e)
            agents = []
        for ident in agents:
            if not ident.address or not ident.private_key:
                continue
            try:
                sig = sign_digest(ident.private_key, prepared.digest)
            except Exception as e:
                logger.warning("anchor signing failed for %s: %s",
                               ident.address, e)
                continue
            self._store_sig(digest_hex, sig)
            self._publish_sig(fed.epoch_id, digest_hex, sig)
            signed += 1
        out["signatures_published"] = signed
        return prepared

    def _collect_quorum(self, prepared: Any) -> Dict[str, Any]:
        """Wait (bounded) until the collected signatures meet the anchor
        quorum per Substrate.anchorQuorum. Returns the last status plus
        ``signatures``."""
        digest_hex = prepared.digest.hex()
        deadline = time.monotonic() + max(self.signature_timeout, 0.0)
        status: Dict[str, Any] = {"reached": False}
        seen = -1
        while True:
            sigs = self.signatures_for(digest_hex)
            if len(sigs) != seen:
                seen = len(sigs)
                signers, ordered = order_signatures(prepared.digest, sigs)
                try:
                    # Only registered agents can carry weight; drop the
                    # rest so junk signers cannot crowd the 64-signer cap.
                    reg = self._anchorer.are_registered(signers) \
                        if hasattr(self._anchorer, "are_registered") else None
                    if reg is not None:
                        keep = [i for i, ok in enumerate(reg) if ok]
                        signers = [signers[i] for i in keep]
                        ordered = [ordered[i] for i in keep]
                    status = dict(self._anchorer.anchor_quorum(signers))
                except Exception as e:
                    status = {"reached": False, "error": str(e)}
                status["signatures"] = ordered
                status["signers"] = signers
                if status.get("reached"):
                    return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return status
            with self._sig_cond:
                self._sig_cond.wait(timeout=min(self.poll_interval, remaining))
            # Re-query quorum at least once per poll even without new sigs
            # (AnchorSnapshotNotFinal clears as blocks advance).
            if "error" in status:
                seen = -1

    def _anchor(self, fed: FederatedCloseResult,
                prepared: Optional[Any] = None) -> AnchorResult:
        if prepared is None:
            prepared = self._sign_round(fed, {})
            if prepared is None:
                return AnchorResult(success=False,
                                    error="anchor not prepared/signed")
        try:
            status = self._collect_quorum(prepared)
            if not status.get("reached"):
                err = (
                    f"anchor quorum not reached within "
                    f"{self.signature_timeout:.0f}s (weight="
                    f"{status.get('weight')}, total={status.get('total')}, "
                    f"genesis={status.get('genesis')}, signers="
                    f"{len(status.get('signers') or [])}"
                    + (f", error={status['error']}" if status.get("error") else "")
                    + ")")
                logger.warning("epoch %s: %s", fed.epoch_id, err)
                return AnchorResult(success=False, error=err,
                                    epoch_id=fed.epoch_id)
            result = self._anchorer.submit_prepared(
                prepared, status["signatures"])
        except Exception as e:
            logger.error("anchor failed: %s", e, exc_info=True)
            return AnchorResult(success=False, error=str(e))
        if result.success and result.agent_mint_blob:
            # Make the blob fetchable by per-agent submitters (and,
            # eventually, by peers via the libp2p resolver).
            try:
                self._blob_resolver.put(result.agent_mint_blob)
            except Exception as e:
                logger.warning("blob_resolver.put failed: %s", e)
        if result.success and result.payload_bytes:
            # Publish the encoded authoritative payload too: its cid
            # equals the on-chain payloadHash, which is how rejoining
            # daemons discover world_cid during anchored catch-up.
            try:
                self._blob_resolver.put(result.payload_bytes)
            except Exception as e:
                logger.warning("payload blob put failed: %s", e)
        if result.success:
            logger.info(
                "anchor success: epoch=%s tx=%s cid=%s",
                result.epoch_id, result.tx_hash[:16], result.agent_mint_cid[:16],
            )
        else:
            logger.warning("anchor failed: %s", result.error)
        return result

    def _submit_agents(self, epoch_id: str) -> List[Dict[str, Any]]:
        """Run AuthoritativeChainSubmitter.submit_for_epoch for each
        registered agent on this daemon."""
        try:
            agents = self._agent_resolver()
        except Exception as e:
            logger.warning("agent_chain_resolver failed: %s", e)
            return []

        results: List[Dict[str, Any]] = []
        for ident in agents:
            if not ident.address or not ident.private_key:
                continue
            submitter = self._get_submitter(ident)
            try:
                r = submitter.submit_for_epoch(epoch_id)
            except Exception as e:
                logger.error(
                    "submit_for_epoch raised for %s: %s",
                    ident.address, e, exc_info=True,
                )
                continue
            results.append({
                "agent_address": ident.address,
                "success": r.success,
                "tx_hash": r.tx_hash,
                "raw_mint": r.raw_mint,
                "error": r.error,
            })
        return results

    def _get_submitter(
        self, ident: AgentChainIdentity,
    ) -> AuthoritativeChainSubmitter:
        with self._lock:
            cached = self._submitter_cache.get(ident.address)
            if cached is not None:
                return cached
            sub = AuthoritativeChainSubmitter(
                agent_id=ident.address,
                agent_address=ident.address,
                resolver=self._blob_resolver,
                config=AuthoritativeSubmitterConfig(
                    substrate_address=self.config.substrate_address,
                    rpc_url=self.config.rpc_url,
                    chain_id=self.config.chain_id,
                    private_key=ident.private_key,
                ),
            )
            self._submitter_cache[ident.address] = sub
            return sub
