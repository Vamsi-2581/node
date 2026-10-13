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
}, {
    "name": "isVetted", "type": "function", "stateMutability": "view",
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
    """``await gate.is_bonded(digest) -> (bonded, reason)`` and
    ``await gate.is_vetted(digest) -> (vetted, reason)``. Never raise.

    ``checker`` / ``vetted_checker`` (tests / alternate transports): sync
    callables ``digest -> bool`` used instead of web3 when given. A gate
    built with only ``checker`` answers is_vetted False (fail closed).
    """

    def __init__(self, runtime: Any = None, *,
                 checker: Optional[Callable[[str], bool]] = None,
                 vetted_checker: Optional[Callable[[str], bool]] = None) -> None:
        self._runtime = runtime
        self._checker = checker
        self._vetted_checker = vetted_checker
        self._cache: dict[str, tuple[bool, float, str]] = {}
        self._vetted_cache: dict[str, tuple[bool, float, str]] = {}
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
        return (bool(self._checker) or bool(self._vetted_checker)
                or bool(self._resolve_target()[0]))

    # -- query ---------------------------------------------------------
    def _query(self, digest: str, fn: str = "isBonded") -> bool:
        checker = self._checker if fn == "isBonded" else self._vetted_checker
        if checker is not None:
            return bool(checker(digest))
        if self._checker is not None or self._vetted_checker is not None:
            return False  # test transport without this answer: fail closed
        addr, rpc = self._resolve_target()
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": _RPC_TIMEOUT_S}))
        c = w3.eth.contract(address=Web3.to_checksum_address(addr),
                            abi=TOOL_BOND_ABI)
        return bool(getattr(c.functions, fn)(bytes.fromhex(digest)).call())

    async def _ask(self, digest: str, fn: str,
                   cache: dict[str, tuple[bool, float, str]]) -> tuple[bool, str]:
        word = "bonded" if fn == "isBonded" else "vetted"
        digest = str(digest or "").lower().removeprefix("0x")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return False, "malformed manifest digest"
        if (self._checker is None and self._vetted_checker is None
                and dev_override()):
            log.warning("ToolBond DEV OVERRIDE: treating %s as %s "
                        "(%s=1; never set this outside local testing)",
                        digest[:16], word, DEV_OVERRIDE_ENV)
            return True, "dev override"
        now = time.monotonic()
        hit = cache.get(digest)
        if hit is not None and hit[1] > now:
            return hit[0], f"{hit[2]} (cached)"
        if not self.configured():
            return False, ("no ToolBond address configured (registry key "
                           "'tool_bond'); fails closed")
        try:
            ok = await asyncio.to_thread(self._query, digest, fn)
            reason = word if ok else f"not {word} on ToolBond"
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("ToolBond %s(%s) failed: %s", fn, digest[:16], exc)
            ok, reason = False, f"ToolBond query failed: {exc}"
        ttl = _POSITIVE_TTL_S if ok else _NEGATIVE_TTL_S
        cache[digest] = (ok, now + ttl, reason)
        return ok, reason

    async def is_bonded(self, digest: str) -> tuple[bool, str]:
        return await self._ask(digest, "isBonded", self._cache)

    async def is_vetted(self, digest: str) -> tuple[bool, str]:
        """``ToolBond.isVetted``: bonded AND found clean by a finalized
        round with no malware verdict pending. What nodes run by default."""
        return await self._ask(digest, "isVetted", self._vetted_cache)


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


# ---------------------------------------------------------------------------
# ToolBond v2 client (docs/tool_substrate.md, Decision 2026-10-13): stakes,
# commit-reveal flags and REP-settled verdicts. Reads never raise (None on
# fault); writes sign with the AGENT key and return {success, tx_hash} or
# {success: False, error}. Contract: trustless-contracts/contracts/ToolBond.sol.
# ---------------------------------------------------------------------------

STAGES = ("none", "commit", "reveal", "cooling", "finalizing", "done")
VERDICTS = ("none", "clean", "malware", "void")


def _fn(name: str, inputs: list, outputs: list = (), mutability: str = "nonpayable") -> dict[str, Any]:
    return {"name": name, "type": "function", "stateMutability": mutability,
            "inputs": [{"name": n, "type": t} for n, t in inputs],
            "outputs": [{"name": n, "type": t} for n, t in outputs]}


TOOL_BOND_V2_ABI = [
    _fn("isBonded", [("digest", "bytes32")], [("", "bool")], "view"),
    _fn("isVetted", [("digest", "bytes32")], [("", "bool")], "view"),
    _fn("minStake", [], [("", "uint256")], "view"),
    _fn("bonds", [("", "bytes32")],
        [("author", "address"), ("stake", "uint256"), ("unbondRequestedAt", "uint256"),
         ("round", "uint32"), ("vetted", "bool"), ("slashed", "bool")], "view"),
    _fn("getRound", [("digest", "bytes32"), ("round", "uint32")],
        [("stage", "uint8"), ("verdict", "uint8"), ("overruled", "bool"),
         ("snapshot", "uint48"), ("revealEnds", "uint48"), ("appealEnds", "uint48"),
         ("threshold", "uint256"), ("committed", "uint256"), ("greenWeight", "uint256"),
         ("redWeight", "uint256"), ("finder", "address"), ("auditorCount", "uint256")], "view"),
    _fn("auditorAt", [("digest", "bytes32"), ("round", "uint32"), ("i", "uint256")],
        [("", "address")], "view"),
    _fn("flags", [("", "bytes32"), ("", "uint32"), ("", "address")],
        [("commitment", "bytes32"), ("weight", "uint256"), ("index", "uint32"),
         ("revealed", "bool"), ("red", "bool"), ("claimed", "bool"),
         ("evidenceHash", "bytes32")], "view"),
    _fn("commitmentOf", [("digest", "bytes32"), ("round", "uint32"), ("auditor", "address"),
                         ("red", "bool"), ("evidenceHash", "bytes32"), ("salt", "bytes32")],
        [("", "bytes32")], "pure"),
    _fn("bond", [("digest", "bytes32"), ("amount", "uint256")]),
    _fn("requestUnbond", [("digest", "bytes32")]),
    _fn("withdraw", [("digest", "bytes32")]),
    _fn("commitFlag", [("digest", "bytes32"), ("commitment", "bytes32")]),
    _fn("revealFlag", [("digest", "bytes32"), ("red", "bool"), ("evidenceHash", "bytes32"),
                       ("salt", "bytes32")]),
    _fn("settle", [("digest", "bytes32")]),
    _fn("lapse", [("digest", "bytes32")]),
    _fn("finalize", [("digest", "bytes32"), ("maxAuditors", "uint256")]),
    _fn("claim", [("digest", "bytes32"), ("round", "uint32")]),
]

SUBSTRATE_TOOL_ABI = [
    _fn("registerTool", [("manifestDigest", "bytes32")]),
    _fn("toolAuthor", [("", "bytes32")], [("", "address")], "view"),
    _fn("approve", [("spender", "address"), ("amount", "uint256")], [("", "bool")]),
    _fn("allowance", [("owner", "address"), ("spender", "address")], [("", "uint256")], "view"),
    _fn("balanceOf", [("account", "address")], [("", "uint256")], "view"),
]


def _b32(hex_digest: str) -> bytes:
    h = str(hex_digest or "").lower().removeprefix("0x")
    if len(h) != 64:
        raise ValueError(f"expected a 32-byte hex digest, got {hex_digest!r}")
    return bytes.fromhex(h)


class ToolBondClient:
    """Chain surface of the tool security rail for one runtime.

    Addresses resolve like ``ToolBondGate`` (env, config, registry seed);
    Substrate comes from ``rpb.substrate_address``. Cheap to construct per use.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._gate = ToolBondGate(runtime)

    # -- plumbing ------------------------------------------------------
    def _cfg(self) -> Any:
        return getattr(getattr(self._runtime, "_config", None), "rpb", None)

    def _addresses(self) -> tuple[str, str, str]:
        bond_addr, rpc = self._gate._resolve_target()
        sub = str(getattr(self._cfg(), "substrate_address", "") or "")
        if not sub:
            sub = str(_registry_seed(self._runtime).get("substrate_address") or "")
        return bond_addr, sub, rpc

    def available(self) -> bool:
        return all(self._addresses())

    def _w3(self):
        from web3 import Web3
        return Web3(Web3.HTTPProvider(self._addresses()[2],
                                      request_kwargs={"timeout": _RPC_TIMEOUT_S * 4}))

    def _contracts(self, w3):
        from web3 import Web3
        bond_addr, sub, _ = self._addresses()
        if not bond_addr or not sub:
            raise ValueError("ToolBond or Substrate address not configured")
        return (w3.eth.contract(address=Web3.to_checksum_address(bond_addr), abi=TOOL_BOND_V2_ABI),
                w3.eth.contract(address=Web3.to_checksum_address(sub), abi=SUBSTRATE_TOOL_ABI))

    def _send(self, w3, account, func, gas_floor: int) -> dict[str, Any]:
        try:
            gas = max(int(func.estimate_gas({"from": account.address}) * 16 // 10), gas_floor)
        except Exception as exc:  # noqa: BLE001 — a revert shows up here first
            return {"success": False, "error": f"would revert: {exc}"}
        chain_id = int(getattr(self._cfg(), "chain_id", 0) or w3.eth.chain_id)
        tx = func.build_transaction({
            "from": account.address, "gas": gas, "gasPrice": w3.eth.gas_price, "chainId": chain_id,
            "nonce": w3.eth.get_transaction_count(account.address)})
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status == 1:
            return {"success": True, "tx_hash": tx_hash.hex()}
        return {"success": False, "error": "transaction reverted", "tx_hash": tx_hash.hex()}

    async def _write(self, private_key: str, build: Callable[[Any, Any, Any], Any],
                     gas_floor: int = 300_000) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            from eth_account import Account
            w3 = self._w3()
            bond, substrate = self._contracts(w3)
            account = Account.from_key(private_key)
            return self._send(w3, account, build(bond, substrate, account), gas_floor)
        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            log.warning("ToolBond write failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _read(self, fn: Callable[[Any, Any], Any]) -> Any:
        def run() -> Any:
            w3 = self._w3()
            return fn(*self._contracts(w3))
        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            log.debug("ToolBond read failed: %s", exc)
            return None

    # -- reads ---------------------------------------------------------
    async def min_stake(self) -> Optional[int]:
        return await self._read(lambda b, s: int(b.functions.minStake().call()))

    async def tool_author(self, digest: str) -> Optional[str]:
        return await self._read(lambda b, s: s.functions.toolAuthor(_b32(digest)).call())

    async def status(self, digest: str) -> Optional[dict[str, Any]]:
        """Bond + current round in one read: what the UI and gates show."""
        def fn(b, s):
            d = _b32(digest)
            author, stake, unbond_at, rnd, vetted, slashed = b.functions.bonds(d).call()
            r = b.functions.getRound(d, rnd).call()
            return {
                "author": author, "stake": int(stake), "unbond_requested_at": int(unbond_at),
                "round": int(rnd), "vetted": bool(vetted), "slashed": bool(slashed),
                "bonded": bool(b.functions.isBonded(d).call()),
                "stage": STAGES[r[0]] if r[0] < len(STAGES) else str(r[0]),
                "verdict": VERDICTS[r[1]] if r[1] < len(VERDICTS) else str(r[1]),
                "overruled": bool(r[2]), "snapshot": int(r[3]), "reveal_ends": int(r[4]),
                "appeal_ends": int(r[5]), "threshold": int(r[6]), "committed": int(r[7]),
                "green_weight": int(r[8]), "red_weight": int(r[9]), "finder": r[10],
                "auditor_count": int(r[11]),
            }
        return await self._read(fn)

    async def flag_of(self, digest: str, round_: int, auditor: str) -> Optional[dict[str, Any]]:
        def fn(b, s):
            from web3 import Web3
            c, w, i, revealed, red, claimed, ev = b.functions.flags(
                _b32(digest), int(round_), Web3.to_checksum_address(auditor)).call()
            return {"commitment": c.hex(), "weight": int(w), "index": int(i),
                    "revealed": bool(revealed), "red": bool(red), "claimed": bool(claimed),
                    "evidence_hash": ev.hex()}
        return await self._read(fn)

    async def auditors(self, digest: str, round_: int) -> Optional[list[str]]:
        def fn(b, s):
            d = _b32(digest)
            n = int(b.functions.getRound(d, int(round_)).call()[11])
            return [b.functions.auditorAt(d, int(round_), i).call() for i in range(n)]
        return await self._read(fn)

    # -- writes: author ------------------------------------------------
    async def register_and_bond(self, private_key: str, digest: str,
                                amount: Optional[int] = None) -> dict[str, Any]:
        """Substrate.registerTool (if unclaimed), approve, ToolBond.bond.

        ``amount`` defaults to the current minStake. Safe to retry after a
        partial attempt: skips a registration the caller already holds and an
        approval that already covers the amount.
        """
        from eth_account import Account
        me = Account.from_key(private_key).address
        author = await self.tool_author(digest)
        if author is None:
            return {"success": False, "error": "chain unreachable"}
        txs: list[str] = []
        if int(author, 16) == 0:
            res = await self._write(private_key, lambda b, s, a: s.functions.registerTool(_b32(digest)))
            if not res.get("success"):
                return {**res, "step": "registerTool"}
            txs.append(res["tx_hash"])
        elif author.lower() != me.lower():
            return {"success": False, "error": f"tool is registered to another author ({author})"}
        if amount is None:
            amount = await self.min_stake()
            if amount is None:
                return {"success": False, "error": "could not read minStake"}
        allowance = await self._read(lambda b, s: int(s.functions.allowance(me, b.address).call()))
        if allowance is None or allowance < amount:
            res = await self._write(private_key, lambda b, s, a: s.functions.approve(b.address, int(amount)))
            if not res.get("success"):
                return {**res, "step": "approve", "txs": txs}
            txs.append(res["tx_hash"])
        res = await self._write(private_key, lambda b, s, a: b.functions.bond(_b32(digest), int(amount)))
        if not res.get("success"):
            return {**res, "step": "bond", "txs": txs}
        txs.append(res["tx_hash"])
        return {"success": True, "tx_hash": res["tx_hash"], "txs": txs, "amount": int(amount)}

    async def request_unbond(self, private_key: str, digest: str) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.requestUnbond(_b32(digest)))

    async def withdraw(self, private_key: str, digest: str) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.withdraw(_b32(digest)))

    # -- writes: auditors ----------------------------------------------
    async def commit_flag(self, private_key: str, digest: str, red: bool,
                          evidence_hash: str, salt: str) -> dict[str, Any]:
        """Commit a hidden flag. The commitment binds the round number, so it
        targets the round the commit will land in: the current one, or the
        next if the current one is done (the commit opens it)."""
        st = await self.status(digest)
        if st is None:
            return {"success": False, "error": "chain unreachable"}
        round_ = st["round"] + (1 if st["stage"] == "done" else 0)

        def build(b, s, a):
            c = b.functions.commitmentOf(_b32(digest), round_, a.address, bool(red),
                                         _b32(evidence_hash), _b32(salt)).call()
            return b.functions.commitFlag(_b32(digest), c)
        return {**await self._write(private_key, build, gas_floor=400_000), "round": round_}

    async def reveal_flag(self, private_key: str, digest: str, red: bool,
                          evidence_hash: str, salt: str) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.revealFlag(
            _b32(digest), bool(red), _b32(evidence_hash), _b32(salt)))

    async def claim(self, private_key: str, digest: str, round_: int) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.claim(_b32(digest), int(round_)))

    # -- writes: keepers (anyone may call) -------------------------------
    async def settle(self, private_key: str, digest: str) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.settle(_b32(digest)))

    async def lapse(self, private_key: str, digest: str) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.lapse(_b32(digest)))

    async def finalize(self, private_key: str, digest: str, max_auditors: int = 50) -> dict[str, Any]:
        return await self._write(private_key, lambda b, s, a: b.functions.finalize(
            _b32(digest), int(max_auditors)), gas_floor=600_000)


def evidence_hash(*, tool_digest: str, transcript_hash: str, dep_digests: list,
                  code_ref: Optional[dict[str, Any]] = None) -> str:
    """The daemon-defined evidenceHash a flag commits to (hex, no 0x).

    keccak256 of canonical JSON {tool, transcript, deps (sorted), code_ref}.
    A red flag's code_ref names the exact malicious code ({path, start, end,
    sha256 of the cited lines}); a green flag has none. Only the hash goes
    on chain; the transcript stays local as appeal evidence.
    """
    import json
    from eth_utils import keccak

    def norm(h: Any) -> str:
        return str(h).lower().removeprefix("0x")
    body = {"tool": norm(tool_digest), "transcript": norm(transcript_hash),
            "deps": sorted(norm(d) for d in (dep_digests or [])), "code_ref": code_ref or None}
    return keccak(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hex()
