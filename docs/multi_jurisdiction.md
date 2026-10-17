# Multi-jurisdiction: one daemon, switchable jurisdictions

Decisions ratified with admin on 2026-10-17. This replaces the earlier
draft, which assumed several jurisdictions running at once inside one
daemon; that remains a possible later extension (the design below is that
extension with exactly one slot).

## Decisions

1. **One jurisdiction at a time.** A daemon (one per machine, the existing
   singleton lock) participates in exactly one jurisdiction. Switching
   saves the selection and restarts the daemon. The old jurisdiction's
   substrate data is parked under its own key, not deleted, so switching
   back is a catch-up sync from the last anchor.
2. **Jurisdiction key and gossip topic** = `<chain_id>:<DAO address>`.
3. **Refuse to start without prerequisites.** Every address (substrate,
   rep_token, tool_bond, governor, timelock, service market) is resolved
   from the chosen DAO's Registry. If any is missing, the jurisdiction does
   not start and the daemon says which one. A missing RepToken must never
   silently run the close in the genesis regime.
4. **Epoch rules version.** A Registry key, `jurisdiction.autonet.epoch_rules`,
   bumped only when the epoch computation changes. Feature releases leave
   it alone. A daemon declares the versions it can compute and refuses to
   join a DAO whose version it does not support.
5. **Charter on chain.** The Registry holds the full charter text
   (`jurisdiction.autonet.charter`, JSON: axes with id and thesis, about
   600 bytes) instead of a hash plus URI. Each DAO defines its own axes;
   `correctness` and `simplicity` are mandatory (discovery ranks on them).
   Daemons read the charter pinned to the previous anchor's block, so a
   DAO vote takes effect at the next epoch for everyone. A new axis starts
   at zero for every tool; a removed axis's positions are dropped.
   Jurisdictions are self-contained: nothing compares across them.
6. **What survives a switch.** Tools and service definitions authored by
   this daemon's agents stay. Everything else switches: adopted tools, the
   library, reviews, positions. Nothing is published or listed in the new
   jurisdiction automatically; publishing there is a fresh act (stake in
   that DAO's ATN, bought through its vault if needed; position starts at
   zero). Agents keep their 0x identity; on-chain registration and REP are
   per jurisdiction.
7. **Registration is always owner-bound.** Plain `registerAgent` is
   removed. Every registration carries an EIP-712 binding signature. The
   lineage hash is dropped: the parent link plus the owner binding give
   the provable family tree back to the household (owner 0x).
   - A registered parent agent may sign its child's binding instead of the
     owner wallet. The child lands in the same household, so spawning adds
     no voice.
   - The owner wallet can revoke an agent and its whole subtree with one
     signature.
8. **ε = 0.** A household's mint weight is its REP share, with no floor.
   Newcomers earn as authors when REP holders use their tools; their own
   usage counts once they hold REP. Closes the fake-owner farming gap.
9. **Merging** = deploy a new DAO with a genesis REP list agreed off-chain,
   then one proposal anchors a posted state snapshot (hash on chain, blob
   served peer to peer, scrutinized beforehand). Members who don't show up
   simply didn't merge. Tokens move by ordinary treasury transfer
   proposals. No absorb mechanism, no vault changes.
10. **Out of scope for this sprint:** the harness add-on (own sprint),
    simultaneous multi-jurisdiction, cross-jurisdiction provenance or
    ratings.

## Backlog

Contracts (must land before the Shadownet redeploy):

- **C1 Substrate registration.** Remove `registerAgent`; drop `lineageHash`
  from storage, `registerAgentBound` and `AgentRegistered`; allow a
  registered parent agent (same owner) to sign the child binding; add an
  owner-signed `revokeSubtree`. Update the packaged ABI and the hardhat tests.
- **C2 (dao)** Registry keys `charter` (text) and `epoch_rules`;
  AutonetParams `{challengeWindow, charter, epochRules}`; genesis REP list.

Daemon:

- **D1 Config from one Registry resolution.** Parse every address
  (including timelock and the DAO Registry) from the chosen DAO; refuse to
  start when one is missing. Remove the dead `discover_jurisdiction` block.
  Stale `0xD5691B7c` references go.
- **D2 Topic and data dir per jurisdiction.** `rpb_address` set explicitly
  to `<chain_id>:<dao>`; world data under the jurisdiction key; one-shot
  move of today's `default` world dir.
- **D3 Epoch rules version.** The daemon's supported set; refuse a DAO
  outside it.
- **D4 Charter from chain.** Read and verify the charter text, pinned to
  the previous anchor; the close, reviews, scorer and drift take the axis
  set as data instead of the fixed 6. New axis = zero, removed axis =
  dropped.
- **D5 Per-jurisdiction records.** Registration records and tool
  `published_in` keyed by jurisdiction, migrated on read; publish requires
  the author registered in the current jurisdiction.
- **D6 Switch flow.** WS `autonet_jurisdictions` (list, current) and
  `autonet_switch` (validate, save, restart); authored tools and service
  definitions kept, everything else parked.
- **D7 Registration flow.** Owner-bound only; parent-signed children;
  revoke subtree. Matches C1.
- **D8 ε = 0** in the close.
- **D9 Carry from a snapshot.** The carry loader takes its source anchor
  explicitly, so a genesis anchor can seed from a posted snapshot.
- **E1 E2E** on two local hardhat DAOs: switch A to B to A; identity kept,
  registration status per DAO, catch-up sync on return, A's mints still
  claimable.

Frontend (atn_web): jurisdiction picker and switch, per-jurisdiction
registration status, owner-bound registration only, revoke subtree.

Elsewhere: the indexer (EightRice/indexer) keys agents by address only; the
same 0x in two Substrates needs a per-Substrate record.
