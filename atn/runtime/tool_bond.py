"""ToolBond gate for core-replacing tools (docs/tool_substrate.md,
Decision 2026-10-12).

A pinned tool may replace a core built-in (shell / file tools) only while
its manifest digest is BONDED on-chain: ``ToolBond.isBonded(bytes32)`` is
true while the author's stake >= minStake, no unbond is pending, and the
tool was never slashed (contract: trustless-contracts/contracts/ToolBond.sol).
The bond is what an accuser can take: ``accuse(digest, evidenceCid)`` with
the daemon-signed evidence trace (shell_provider.capture_evidence).

FAIL CLOSED. The address comes from the jurisdiction registry
(``contracts.tool_bond`` in atn/registry.json, or ATN_REGISTRY_URL), or the
ATN_TOOL_BOND_ADDRESS env var. With no address, or any RPC fault, the answer
is "not bonded" and the swap is refused. The single exception is the dev
override ``ATN_TOOL_BOND_DEV_OVERRIDE=1`` for local testing, which treats
every digest as bonded and logs a warning each time it is consulted.

Results are cached briefly (positive 60s, negative 15s) so a dispatch hot
path does not hit the RPC per call. The web3 call runs in a thread.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

DEV_OVERRIDE_ENV = "ATN_TOOL_BOND_DEV_OVERRIDE"
ADDRESS_ENV = "ATN_TOOL_BOND_ADDRESS"
_POSITIVE_TTL_S = 60.0
_NEGATIVE_TTL_S = 15.0
_RPC_TIMEOUT_S = 5.0

TOOL_BOND_ABI = [{
    "name": "isBonded", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "digest", "type": "bytes32"}],
    "outputs": [{"name": "", "type": "bool"}],
}]


def dev_override() -> bool:
    return os.environ.get(DEV_OVERRIDE_ENV, "").strip() == "1"


def _registry_seed(runtime: Any) -> dict[str, Any]:
    try:
        from ..config import _load_registry_seed
        cfg = getattr(getattr(runtime, "_config", None), "rpb", None)
        jid = str(getattr(cfg, "jurisdiction_id", "") or "autonet")
        return _load_registry_seed(jid)
    except Exception:  # noqa: BLE001
        return {}


class ToolBondGate:
    """``await gate.is_bonded(digest) -> (bonded, reason)``. Never raises.

    ``checker`` (tests / alternate transports): a sync callable
    ``digest -> bool`` used instead of web3 when given.
    """

    def __init__(self, runtime: Any = None, *,
                 checker: Optional[Callable[[str], bool]] = None) -> None:
        self._runtime = runtime
        self._checker = checker
        self._cache: dict[str, tuple[bool, float, str]] = {}
        self._address: Optional[str] = None
        self._rpc_url: Optional[str] = None

    # -- configuration -------------------------------------------------
    def _resolve_target(self) -> tuple[str, str]:
        if self._address is None:
            addr = os.environ.get(ADDRESS_ENV, "").strip()
            rpc = ""
            cfg = getattr(getattr(self._runtime, "_config", None), "rpb", None)
            if not addr:
                addr = str(getattr(cfg, "tool_bond_address", "") or "")
            seed: dict[str, Any] = {}
            if not addr:
                seed = _registry_seed(self._runtime)
                addr = str(seed.get("tool_bond_address") or "")
            rpc = str(getattr(cfg, "rpc_url", "") or seed.get("rpc_url") or "")
            if addr and not rpc:
                rpc = str(_registry_seed(self._runtime).get("rpc_url") or "")
            self._address, self._rpc_url = addr, rpc
        return self._address or "", self._rpc_url or ""

    def configured(self) -> bool:
        return bool(self._checker) or bool(self._resolve_target()[0])

    # -- query ---------------------------------------------------------
    def _query(self, digest: str) -> bool:
        if self._checker is not None:
            return bool(self._checker(digest))
        addr, rpc = self._resolve_target()
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": _RPC_TIMEOUT_S}))
        c = w3.eth.contract(address=Web3.to_checksum_address(addr),
                            abi=TOOL_BOND_ABI)
        return bool(c.functions.isBonded(bytes.fromhex(digest)).call())

    async def is_bonded(self, digest: str) -> tuple[bool, str]:
        digest = str(digest or "").lower().removeprefix("0x")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return False, "malformed manifest digest"
        if self._checker is None and dev_override():
            log.warning("ToolBond DEV OVERRIDE: treating %s as bonded "
                        "(%s=1; never set this outside local testing)",
                        digest[:16], DEV_OVERRIDE_ENV)
            return True, "dev override"
        now = time.monotonic()
        hit = self._cache.get(digest)
        if hit is not None and hit[1] > now:
            return hit[0], f"{hit[2]} (cached)"
        if not self.configured():
            return False, ("no ToolBond address configured (registry key "
                           "'tool_bond'); core replacement fails closed")
        try:
            bonded = await asyncio.to_thread(self._query, digest)
            reason = "bonded" if bonded else "not bonded on ToolBond"
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("ToolBond isBonded(%s) failed: %s", digest[:16], exc)
            bonded, reason = False, f"ToolBond query failed: {exc}"
        ttl = _POSITIVE_TTL_S if bonded else _NEGATIVE_TTL_S
        self._cache[digest] = (bonded, now + ttl, reason)
        return bonded, reason


def gate_for(runtime: Any) -> ToolBondGate:
    """The runtime's gate (created lazily, so tests can pre-set one)."""
    gate = getattr(runtime, "tool_bond_gate", None)
    if isinstance(gate, ToolBondGate):
        return gate
    gate = ToolBondGate(runtime)
    try:
        setattr(runtime, "tool_bond_gate", gate)
    except Exception:  # noqa: BLE001
        pass
    return gate
