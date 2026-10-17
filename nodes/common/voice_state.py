"""Household voice state — chain-derived owner map + voice weights.

Spec: docs/tool_substrate.md, Decision 2026-07-08 ("ATN = money,
reputation = voice") as amended by Decision 2026-07-10 (fees-only
emission + REP-from-earnings). The federated close collapses callers to
HOUSEHOLDS (proven owner wallet, per Substrate.sol's EIP-712 owner
binding) and scales each household's damped usage/review credit by a
voice weight that is LINEAR in the household's REPUTATION:

    weight(house) = epsilon + household_reputation / reputationTotalSupply

where household_reputation = Σ reputation of every agent bound to that
owner. Linearity is the sybil property: splitting reputation across any
number of agents never gains weight; ``epsilon`` bounds what a
zero-reputation identity can carry and lets a cold-start network
(supply == 0) bootstrap.

REP SOURCE = RepToken (DAO), Decision 2026-07-10. Substrate.sol is now a
PURE MONEY contract — its reputation surface (``reputationOfAt`` /
``reputationTotalSupplyAt`` / ``agentReputation``) is DELETED. Reputation
lives DAO-side in RepToken, claimed by agents on their ratified ATN
earnings. We read each household's REP share from RepToken pinned to the
previous epoch's anchor. RepToken is TIMESTAMP-clocked (ERC20Votes,
``mode=timestamp``), so we pin on the anchor's ``timestamp`` (Anchor
struct field 8), not its block number, using the standard vote
checkpoints ``getPastVotes(addr, ts)`` / ``getPastTotalSupply(ts)``.
NOTE (judgment call, flagged): RepToken exposes no public historical
per-account BALANCE getter; the only public pinned per-account surface is
``getPastVotes``, i.e. delegated VOTING POWER. That is the correct voice
semantic — REP is the governance/voice token, and voice == voting power —
but a holder who auto-delegates to a default delegate rolls their voice
into that delegate. If per-holder rep share (independent of delegation)
is ever required, RepToken needs a public ``getPastBalance``.

OWNER-WALLET REPUTATION NOTE: there is NO separate owner-wallet term. A
bare owner wallet that never earned/claimed REP has zero votes; if the
owner ADDRESS is itself a registered agent it already appears in the
agent set and its REP is summed into its household there.

Determinism contract: every input is derived AS OF the previous epoch's
anchor (``getAnchor(anchorCount-1)`` — block number for logs, timestamp
for RepToken checkpoints, both stored on-chain at submission), so all
daemons price this epoch's voices from the identical snapshot no matter
when their refresh fires. NO ARCHIVE NODE REQUIRED: RepToken vote
checkpoints serve historical reads from current state; the agent set +
owner map derive from ``AgentRegistered`` / ``OwnerBound`` event logs up
to the snapshot block (last binding per agent wins).

The emission pool is FEES-ONLY (Decision 2026-07-10): the burned
ServiceFee shares in the snapshot anchor's window, no base floor. Since
2026-10-12 it is read from the anchor struct's ``windowBurned`` (the exact
quantity Substrate caps the next epoch's mints at), not summed from logs. Zero
service volume => zero pool => zero mint. It is MONEY, ATN-denominated.

No prior anchor = no agreed snapshot: the refresh returns empty maps and
the close runs with weights=None (the uniform pre-voice behavior) —
correct for epoch 1, where nothing has minted yet anyway. No RepToken
address configured => empty rep (genesis regime), pool still fees-only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .federated_reconcile import AUDITOR_SHARE_BPS_DEFAULT, VOICE_EPSILON

logger = logging.getLogger(__name__)

# RepToken (DAO) read surface: ERC20Votes checkpoints, timestamp-clocked.
_REP_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "account",   "type": "address"},
            {"internalType": "uint256", "name": "timepoint", "type": "uint256"},
        ],
        "name": "getPastVotes",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "timepoint", "type": "uint256"}],
        "name": "getPastTotalSupply",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_VOICE_ABI = [
    {
        "inputs": [],
        "name": "anchorCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "index", "type": "uint256"}],
        "name": "getAnchor",
        "outputs": [{
            "components": [
                {"internalType": "string",  "name": "epochId",       "type": "string"},
                {"internalType": "bytes32", "name": "epochRoot",     "type": "bytes32"},
                {"internalType": "bytes32", "name": "prevEpochRoot", "type": "bytes32"},
                {"internalType": "bytes32", "name": "prevAnchorHash","type": "bytes32"},
                {"internalType": "string",  "name": "agentMintCid",  "type": "string"},
                {"internalType": "bytes32", "name": "payloadHash",   "type": "bytes32"},
                {"internalType": "address", "name": "submitter",     "type": "address"},
                {"internalType": "uint256", "name": "blockNumber",   "type": "uint256"},
                {"internalType": "uint256", "name": "timestamp",     "type": "uint256"},
                {"internalType": "bytes32", "name": "agentMintRoot", "type": "bytes32"},
                {"internalType": "uint256", "name": "windowBurned",  "type": "uint256"},
                {"internalType": "uint256", "name": "mintPool",      "type": "uint256"},
                {"internalType": "uint256", "name": "minted",        "type": "uint256"},
            ],
            "internalType": "struct Substrate.Anchor",
            "name": "",
            "type": "tuple",
        }],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "agent",       "type": "address"},
            {"indexed": False, "internalType": "bytes",   "name": "peerId",      "type": "bytes"},
            {"indexed": False, "internalType": "uint256", "name": "timestamp",   "type": "uint256"},
        ],
        "name": "AgentRegistered",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "agent", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
        ],
        "name": "AgentRevoked",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "agent",     "type": "address"},
            {"indexed": True,  "internalType": "address", "name": "owner",     "type": "address"},
            {"indexed": True,  "internalType": "address", "name": "parent",    "type": "address"},
            {"indexed": False, "internalType": "address", "name": "prevOwner", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "nonce",     "type": "uint256"},
        ],
        "name": "OwnerBound",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "payer",      "type": "address"},
            {"indexed": True,  "internalType": "address", "name": "recipient",  "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount",     "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "burned",     "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "toTreasury", "type": "uint256"},
        ],
        "name": "ServiceFee",
        "type": "event",
    },
]

# Chain amounts are ATN x 1e6 (the agent-side submitter scaling);
# emission pools are float ATN units on the close side.
_ATN_SCALE = 1_000_000.0

_ZERO = "0x0000000000000000000000000000000000000000"


# (rpc_url-ish key, address) -> deployment block. Found once per
# process; the contract's creation block never changes.
_DEPLOY_BLOCK_CACHE: Dict[Any, int] = {}


def _deployment_block(w3, address: str) -> int:
    """Lowest block at which the contract has code (binary search on
    eth_getCode, ~log2(head) RPC calls, cached). Event scans start
    here instead of block 0 — public RPCs cap eth_getLogs ranges."""
    key = (id(w3.provider), address.lower())
    hit = _DEPLOY_BLOCK_CACHE.get(key)
    if hit is not None:
        return hit
    lo, hi = 0, w3.eth.block_number
    if not w3.eth.get_code(address, block_identifier=hi):
        return hi  # no code at head — nothing to scan anyway
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            has_code = bool(w3.eth.get_code(address, block_identifier=mid))
        except Exception:
            # Non-archive node can't serve old getCode — assume no code
            # there and keep moving up; worst case we start the scan a
            # little late, never early.
            has_code = False
        if has_code:
            hi = mid
        else:
            lo = mid + 1
    _DEPLOY_BLOCK_CACHE[key] = lo
    return lo


def _get_logs_range(event, from_block: int, to_block: int):
    try:
        return event.get_logs(from_block=from_block, to_block=to_block)
    except TypeError:
        return event.get_logs(fromBlock=from_block, toBlock=to_block)


def _get_logs(event, w3, address: str, to_block: int):
    """Fetch an event's logs [deployment, to_block], splitting the
    range adaptively when the RPC rejects it as too large (public
    endpoints cap eth_getLogs spans)."""
    start = _deployment_block(w3, address)
    if start > to_block:
        return []
    spans = [(start, to_block)]
    out = []
    while spans:
        lo, hi = spans.pop()
        try:
            out.extend(_get_logs_range(event, lo, hi))
        except Exception as e:
            if lo >= hi or "range" not in str(e).lower():
                raise
            mid = (lo + hi) // 2
            spans.append((mid + 1, hi))
            spans.append((lo, mid))
    return out


# ---------------------------------------------------------------------------
# ToolBond vetting snapshot (docs/tool_substrate.md, Decision 2026-10-13).
#
# The close gives a VETTED tool's auditors (the green flaggers of the round
# that vetted it) ``auditorShareBps`` of the tool's mint, split by flag
# weight. That needs ToolBond state AS OF the same snapshot as the voice
# weights. ToolBond keeps no checkpoints, and an eth_call at the anchor
# block needs archive state (a non-archive daemon would fall back and fork
# from an archive one), so the state is REBUILT from ToolBond's event logs
# up to the snapshot block, exactly like the owner map above: deterministic
# chain history, served by non-archive nodes. The fold mirrors the
# contract's storage writes:
#
#   isBonded  = !slashed && no pending unbond && stake > 0 && stake >= minStake
#   isVetted  = isBonded && vetted && no Malware verdict pending on the
#               current round (VerdictReached/Overruled Malware, not yet
#               Finalized: isVetted turns false as soon as it settles)
#   auditors  = revealed green flaggers of the latest round Finalized Clean,
#               weight = FlagCommitted.weight (== flags(...).weight)
#   bps       = auditorShareBps of the last ParamsSet (the constructor emits
#               one); older deployments have no such event => default.
# ---------------------------------------------------------------------------

def _ev(name: str, fields: list) -> Dict[str, Any]:
    return {
        "anonymous": False, "name": name, "type": "event",
        "inputs": [{"indexed": idx, "internalType": t, "name": n, "type": t}
                   for n, t, idx in fields],
    }


_TOOL_BOND_EVENTS_ABI = [
    _ev("Bonded", [("digest", "bytes32", True), ("author", "address", True),
                   ("amount", "uint256", False), ("stake", "uint256", False)]),
    _ev("UnbondRequested", [("digest", "bytes32", True),
                            ("availableAt", "uint256", False)]),
    _ev("Unbonded", [("digest", "bytes32", True), ("author", "address", True),
                     ("amount", "uint256", False)]),
    _ev("FlagCommitted", [("digest", "bytes32", True), ("round", "uint32", True),
                          ("auditor", "address", True), ("weight", "uint256", False)]),
    _ev("FlagRevealed", [("digest", "bytes32", True), ("round", "uint32", True),
                         ("auditor", "address", True), ("red", "bool", False),
                         ("evidenceHash", "bytes32", False)]),
    _ev("VerdictReached", [("digest", "bytes32", True), ("round", "uint32", True),
                           ("verdict", "uint8", False), ("appealEnds", "uint48", False)]),
    _ev("Overruled", [("digest", "bytes32", True), ("round", "uint32", True),
                      ("verdict", "uint8", False)]),
    _ev("Finalized", [("digest", "bytes32", True), ("round", "uint32", True),
                      ("verdict", "uint8", False), ("repPool", "uint256", False),
                      ("atnPool", "uint256", False)]),
    _ev("ParamsSet", [("minStake", "uint256", False), ("thresholdBps", "uint256", False),
                      ("reviewWindow", "uint256", False), ("revealWindow", "uint256", False),
                      ("finderBps", "uint256", False), ("degradeBps", "uint256", False),
                      ("authorSlashBps", "uint256", False), ("unbondDelay", "uint256", False),
                      ("auditorShareBps", "uint256", False)]),
]

# ToolBond.Verdict enum
_VERDICT_CLEAN = 1
_VERDICT_MALWARE = 2


def _hex32(value: Any) -> str:
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return str(raw).lower().removeprefix("0x")


def tool_vetting_snapshot_from_logs(
    records: List[Dict[str, Any]],
    *,
    default_bps: int = AUDITOR_SHARE_BPS_DEFAULT,
) -> Dict[str, Any]:
    """Fold ToolBond event records into the close's vetting snapshot.

    ``records``: ``{"block", "index", "event", "args"}`` dicts (any order;
    folded in (block, logIndex) order). Pure, so it is testable without a
    chain. Returns {"auditor_share_bps": int, "tools": {digest: {"round":
    int, "auditors": {address_lower: weight}}}} listing only vetted tools
    with at least one weighted green auditor.
    """
    ordered = sorted(records, key=lambda r: (int(r["block"]), int(r["index"])))
    min_stake = 0
    bps: Optional[int] = None
    bonds: Dict[str, Dict[str, Any]] = {}
    weights: Dict[Tuple[str, int, str], int] = {}
    green: Dict[Tuple[str, int], set] = {}

    def bond(d: str) -> Dict[str, Any]:
        return bonds.setdefault(d, {
            "stake": 0, "unbond": False, "slashed": False, "vetted": False,
            "round": 0, "clean_round": None, "verdicts": {}, "finalized": set(),
        })

    for r in ordered:
        name, a = r["event"], r["args"]
        if name == "ParamsSet":
            min_stake = int(a["minStake"])
            bps = int(a["auditorShareBps"])
            continue
        d = _hex32(a["digest"])
        b = bond(d)
        if "round" in a:
            b["round"] = max(b["round"], int(a["round"]))
        if name == "Bonded":
            b["stake"] = int(a["stake"])
            b["unbond"] = False
        elif name == "UnbondRequested":
            b["unbond"] = True
        elif name == "Unbonded":
            b["stake"], b["unbond"], b["vetted"] = 0, False, False
        elif name == "FlagCommitted":
            key = (d, int(a["round"]), str(a["auditor"]).lower())
            weights[key] = int(a["weight"])
        elif name == "FlagRevealed":
            if not bool(a["red"]):
                green.setdefault((d, int(a["round"])), set()).add(
                    str(a["auditor"]).lower())
        elif name in ("VerdictReached", "Overruled"):
            b["verdicts"][int(a["round"])] = int(a["verdict"])
        elif name == "Finalized":
            rnd, verdict = int(a["round"]), int(a["verdict"])
            b["finalized"].add(rnd)
            if verdict == _VERDICT_MALWARE:
                b["stake"], b["unbond"] = 0, False
                b["slashed"], b["vetted"] = True, False
            elif verdict == _VERDICT_CLEAN:
                b["vetted"] = True
                b["clean_round"] = rnd

    tools: Dict[str, Any] = {}
    for d in sorted(bonds):
        b = bonds[d]
        bonded = (not b["slashed"] and not b["unbond"] and b["stake"] > 0
                  and b["stake"] >= min_stake)
        cur = b["round"]
        malware_pending = (b["verdicts"].get(cur) == _VERDICT_MALWARE
                           and cur not in b["finalized"])
        if not (bonded and b["vetted"]) or malware_pending:
            continue
        rnd = b["clean_round"]
        auditors = {
            addr: weights.get((d, rnd, addr), 0)
            for addr in sorted(green.get((d, rnd), set()))
        }
        auditors = {k: v for k, v in auditors.items() if v > 0}
        if auditors:
            tools[d] = {"round": int(rnd), "auditors": auditors}
    return {
        "auditor_share_bps": int(default_bps if bps is None else bps),
        "tools": tools,
    }


def read_tool_vetting_snapshot(
    w3: Any, tool_bond_address: str, block: int,
) -> Dict[str, Any]:
    """ToolBond vetting snapshot as of ``block`` (the snapshot anchor's
    block), rebuilt from event logs. Raises on RPC failure, like the rest
    of the voice read: the driver keeps its previous snapshot."""
    from web3 import Web3

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(tool_bond_address),
        abi=_TOOL_BOND_EVENTS_ABI,
    )
    records: List[Dict[str, Any]] = []
    for spec in _TOOL_BOND_EVENTS_ABI:
        event = getattr(contract.events, spec["name"])
        for log in _get_logs(event, w3, contract.address, block):
            records.append({
                "block": int(log["blockNumber"]),
                "index": int(log["logIndex"]),
                "event": spec["name"],
                "args": dict(log["args"]),
            })
    return tool_vetting_snapshot_from_logs(records)


# ---------------------------------------------------------------------------
# Pinned charter (docs/multi_jurisdiction.md decision 5).
#
# The DAO Registry holds the charter text under jurisdiction.autonet.charter
# and emits ``RegistryUpdated(string key, string value)`` (both fields
# non-indexed) on every write. The charter in force for an epoch is the last
# value written at or before the snapshot anchor's block, rebuilt from those
# logs like the owner map (non-archive nodes serve them).
# ---------------------------------------------------------------------------

CHARTER_REGISTRY_KEY = "jurisdiction.autonet.charter"

_REGISTRY_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "internalType": "string", "name": "key", "type": "string"},
            {"indexed": False, "internalType": "string", "name": "value", "type": "string"},
        ],
        "name": "RegistryUpdated",
        "type": "event",
    },
]


def charter_text_from_logs(
    records: List[Dict[str, Any]], block: int,
    key: str = CHARTER_REGISTRY_KEY,
) -> Optional[str]:
    """Last ``value`` written for ``key`` at or before ``block``.

    ``records`` are ``{"block", "index", "key", "value"}``; ordered by
    (block, logIndex). Returns None when the key was never written, or when
    the last value is not charter text (a legacy 0x hash)."""
    last: Optional[str] = None
    for rec in sorted(records, key=lambda r: (int(r["block"]), int(r["index"]))):
        if int(rec["block"]) > int(block):
            break
        if str(rec.get("key")) == key:
            last = str(rec.get("value") or "")
    if last is None or not last.lstrip().startswith("{"):
        return None
    return last


def read_pinned_charter_text(
    w3: Any, dao_registry_address: str, block: int,
    key: str = CHARTER_REGISTRY_KEY,
) -> Optional[str]:
    """The jurisdiction's charter text as of ``block`` (the snapshot
    anchor's block), from the DAO Registry's RegistryUpdated logs. Raises
    on RPC failure, like the rest of the voice read."""
    from web3 import Web3

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(dao_registry_address),
        abi=_REGISTRY_EVENTS_ABI,
    )
    records = [
        {
            "block": int(log["blockNumber"]),
            "index": int(log["logIndex"]),
            "key": log["args"]["key"],
            "value": log["args"]["value"],
        }
        for log in _get_logs(
            contract.events.RegistryUpdated, w3, contract.address, block)
    ]
    return charter_text_from_logs(records, block, key)


def read_voice_state(
    substrate_address: str,
    rpc_url: str = "",
    *,
    epsilon: float = VOICE_EPSILON,
    rep_token_address: str = "",
    tool_bond_address: str = "",
    dao_registry_address: str = "",
    web3: Any = None,
) -> Dict[str, Any]:
    """Read {owner_map, voice_weights, rep_shares, rep_supply, supply,
    snapshot_block, emission_pool, recycled, weight_source,
    vetting_snapshot} as of the previous epoch's anchor.

    ``vetting_snapshot`` (Decision 2026-10-13): ToolBond vetted tools and
    their auditors, rebuilt from ToolBond logs up to the SAME snapshot
    block as the owner map (``read_tool_vetting_snapshot``). Empty
    (``tools == {}``) when ``tool_bond_address`` is unset or at genesis.

    ``charter_text`` (docs/multi_jurisdiction.md decision 5): the DAO
    Registry's ``jurisdiction.autonet.charter`` value as of the SAME
    snapshot block (``read_pinned_charter_text``). None when
    ``dao_registry_address`` is unset, at genesis, or never written.

    ``emission_pool`` (Decision 2026-07-10, fees-only) = the burned fee
    shares (ServiceFee logs) in the snapshot anchor's window — NO base
    floor. Zero service volume => zero pool. MONEY, ATN-denominated.

    Voice weights are REPUTATION-based, read from RepToken (DAO) pinned to
    the anchor TIMESTAMP (RepToken is ERC20Votes, mode=timestamp — see
    module docstring). ``rep_supply`` is the REPUTATION total supply
    (``getPastTotalSupply``); ``weight_source`` is the literal
    ``"reputation"``. ``rep_token_address`` empty => genesis regime: no
    REP configured, empty rep maps, pool still fees-only.

    ``owner_map``: agent address -> owner wallet (lowercased 0x), only for
    agents with a bound owner at the snapshot. ``voice_weights``:
    household key -> epsilon + household_reputation/supply, rounded to 9
    decimals. Raises on RPC failure — the driver's refresh hook catches
    and keeps the previous maps.
    """
    from web3 import Web3

    w3 = web3 if web3 is not None else Web3(Web3.HTTPProvider(rpc_url))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(substrate_address), abi=_VOICE_ABI,
    )

    anchor_count = contract.functions.anchorCount().call()
    if anchor_count == 0:
        # No agreed snapshot yet (epoch 1): no voices, empty fees-only pool.
        return {
            "owner_map": {},
            "voice_weights": {},
            "rep_shares": {},
            "rep_supply": 0,
            "supply": 0,
            "snapshot_block": None,
            "emission_pool": 0.0,
            "recycled": 0.0,
            "emission_pool_raw": 0,
            "snapshot_anchor_count": 0,
            "weight_source": "reputation",
            "vetting_snapshot": {
                "auditor_share_bps": AUDITOR_SHARE_BPS_DEFAULT, "tools": {}},
            "charter_text": None,
        }
    anchor = contract.functions.getAnchor(anchor_count - 1).call()
    block = int(anchor[7])       # Anchor.blockNumber (struct field 7)
    timestamp = int(anchor[8])   # Anchor.timestamp (struct field 8)

    # Fees-only emission, read from the SAME on-chain quantity Substrate
    # caps mints with (2026-10-12): the snapshot anchor's windowBurned =
    # fees burned between the previous anchor and it (tx-order exact).
    # Substrate sets the next anchor's mintPool to exactly this value, so
    # the close's pool and the on-chain cap cannot disagree.
    burned_raw = int(anchor[10]) if len(anchor) > 10 else 0
    recycled = burned_raw / _ATN_SCALE

    # Agent set + owner map from event logs up to the snapshot block —
    # deterministic (chain history) and served by non-archive nodes.
    # Last OwnerBound per agent wins (rotation support), ordered by
    # (blockNumber, logIndex).
    agents = sorted({
        str(log["args"]["agent"]).lower()
        for log in _get_logs(
            contract.events.AgentRegistered, w3, contract.address, block)
    })
    bindings = sorted(
        _get_logs(contract.events.OwnerBound, w3, contract.address, block),
        key=lambda log: (int(log["blockNumber"]), int(log["logIndex"])),
    )
    owner_of: Dict[str, str] = {}
    parent_of: Dict[str, str] = {}
    for log in bindings:
        agent_l = str(log["args"]["agent"]).lower()
        owner_of[agent_l] = str(log["args"]["owner"]).lower()
        parent_of[agent_l] = str(log["args"]["parent"]).lower()

    # Revoked agents drop out with their whole subtree (Substrate.isLive
    # walks the same parent links), as of the snapshot block.
    revoked = {
        str(log["args"]["agent"]).lower()
        for log in _get_logs(
            contract.events.AgentRevoked, w3, contract.address, block)
    }
    if revoked:
        def _live(agent: str) -> bool:
            seen = set()
            a = agent
            while a and a != _ZERO and a not in seen:
                if a in revoked:
                    return False
                seen.add(a)
                a = parent_of.get(a, "")
            return True
        agents = [a for a in agents if _live(a)]

    # Reputation (voice) reads: RepToken (DAO), pinned to the anchor
    # TIMESTAMP (ERC20Votes mode=timestamp). No RepToken configured =>
    # genesis regime: empty rep, close runs drift at weight 1.0.
    owner_map: Dict[str, str] = {}
    for agent in agents:
        owner = owner_of.get(agent, "")
        if owner and owner != _ZERO:
            owner_map[agent] = owner

    supply = 0
    voice_weights: Dict[str, float] = {}
    rep_shares: Dict[str, float] = {}
    if rep_token_address:
        rep = w3.eth.contract(
            address=Web3.to_checksum_address(rep_token_address), abi=_REP_ABI,
        )
        # Denominator is REPUTATION total supply (voice), not ATN.
        supply = int(rep.functions.getPastTotalSupply(timestamp).call())

        def _reputation_at(addr: str) -> int:
            # getPastVotes = delegated VOTING POWER at the pinned timestamp
            # (the only public historical per-account surface RepToken has;
            # voice == voting power — see module docstring).
            return int(rep.functions.getPastVotes(
                Web3.to_checksum_address(addr), timestamp).call())

        # household key -> raw reputation sum (int, exact — floats only at
        # the final ratio so accumulation order can't jitter the weights).
        house_rep: Dict[str, int] = {}
        for agent in agents:
            owner = owner_of.get(agent, "")
            r = _reputation_at(agent)
            if owner and owner != _ZERO:
                # Bound agent: REP rolls up to the owner household. NO
                # separate owner-wallet term — a bare owner never earns REP;
                # if it is itself a registered agent it is summed via this
                # loop. See module docstring.
                house_rep[owner] = house_rep.get(owner, 0) + r
            else:
                house_rep[agent] = house_rep.get(agent, 0) + r

        # The RAW rep share (no epsilon floor) drives drift weight;
        # voice_weights keeps the epsilon floor for mint bootstrap.
        for house in sorted(house_rep.keys()):
            share = (house_rep[house] / supply) if supply > 0 else 0.0
            voice_weights[house] = round(epsilon + share, 9)
            rep_shares[house] = round(share, 9)

    # ToolBond vetting snapshot, pinned to the same anchor block.
    vetting_snapshot: Dict[str, Any] = {
        "auditor_share_bps": AUDITOR_SHARE_BPS_DEFAULT, "tools": {}}
    if tool_bond_address:
        vetting_snapshot = read_tool_vetting_snapshot(
            w3, tool_bond_address, block)

    # The charter in force, pinned to the same anchor block.
    charter_text: Optional[str] = None
    if dao_registry_address:
        charter_text = read_pinned_charter_text(
            w3, dao_registry_address, block)

    return {
        "owner_map": owner_map,
        "voice_weights": voice_weights,
        # Raw rep share per household (un-floored) + supply alias.
        "rep_shares": rep_shares,
        "rep_supply": int(supply),
        # Reputation supply — the voice-weight denominator (not ATN).
        "supply": int(supply),
        "snapshot_block": block,
        # Fees-only pool (Decision 2026-07-10): burned fees, no base floor.
        "emission_pool": round(recycled, 10),
        "recycled": round(recycled, 10),
        # Exact on-chain pool (base units) + which anchor it was read at:
        # the anchor this close produces is valid only as anchor number
        # snapshot_anchor_count (else its on-chain mintPool differs).
        "emission_pool_raw": burned_raw,
        "snapshot_anchor_count": int(anchor_count),
        "weight_source": "reputation",
        "vetting_snapshot": vetting_snapshot,
        "charter_text": charter_text,
    }
