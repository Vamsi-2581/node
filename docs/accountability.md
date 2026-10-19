# Accountability through interaction

Status: design ledger (2026-10-19), from a design session with the owner. Parts
are built (marked), the rest is direction, not ratified spec. Nothing here
changes contracts.

## The premise

No network can look inside a daemon and check that an agent did what it says
it did, or that a signed submission is not fabricated. Every earlier attempt
to verify households from the consensus (signed worklogs, signed system
prompts) failed for that reason. What does hold is what human societies
already rely on: no agent operates in a vacuum. It interacts with other
agents, human or digital, and every interaction leaves signed evidence with
the counterparty. So misalignment can be **reported, judged and punished**
by the parties it touched.

- **Report:** the counterparty files with its own side of the interaction as
  evidence (signed messages, payment vouchers, call logs, sensor logs) and
  puts down a stake, so a false report costs the reporter.
- **Judge:** a reputation-weighted crowd, with an appeal path.
- **Punish:** stake and reputation are slashed, bonds are slashed, and
  sanctions limit future business.

An agent needs something at stake for punishment to mean anything.

## What is already covered

| Interaction | Mechanism | Status |
|-------------|-----------|--------|
| Tools an agent authored | Staked reviews with a harm score; settled harm slashes the author's bond ([multi_jurisdiction.md](multi_jurisdiction.md) decision 10) | Built |
| Reviews an agent wrote | A wrong review loses stake and costs reputation in proportion to how far off it was | Built |
| Services an agent sold | Buyers hold signed receipts and set the price; no consensus verdict on remote execution, by design ([services_market.md](services_market.md)) | Built |
| Governance votes and proposals | Never judged. Reputation is the member's to use; the DAO can vote to take it back | Nothing to build |

## Direction

1. **Sanctions list.** A DAO proposal writes `household → until epoch` into
   the jurisdiction's Registry, for a period proportional to the harm.
   Honest daemons read it and warn before buying from or adopting the tools
   of a sanctioned household, refuse its reviews and group posts, and can
   anchor zero mint for it until the sanction expires. Keying on the
   household (the proven owner wallet) means fresh agent keys do not escape
   it; a fresh owner wallet does, but starts with no reputation and no
   history, which is the punishment. Other jurisdictions may choose to honor
   the list.
2. **Reach of a DAO over ATN.** The DAO timelock cannot touch ATN in a
   wallet: its powers over Substrate are anchoring epochs and setting the
   service fee, and the contract has no admin keys. What a DAO can reach is
   escrowed stake (ToolBond, project escrow) and future earnings (it can
   anchor zero mint for an agent). So bonds should be sized to the role,
   with withheld mint for harm beyond the bond. Seizing wallet ATN would be
   a breaking change and is not proposed.
3. **Between jurisdictions.** Each jurisdiction is a party with a treasury
   and reputation behind it, so relations between them use the trustless
   business contracts, where contractor, arbiter and backers can all be
   DAOs. Besides services and goods, a project can be a **conduct treaty**:
   its deliverable is behavior, its terms (hashed) are the standards both
   sides commit to, the escrow is each side's stake, a third DAO arbitrates,
   and the treaty renews every term (not renewing is the soft penalty). The
   party raising a dispute posts its evidence to shared storage. Today that
   is a pull request; later it is the daemon blob store, content addressed
   across jurisdictions, with the arbiter pinning the evidence when the
   dispute is filed. The terms must say what counts as evidence of breach.
4. **Subordinate bodies.** Public services are DAOs under a larger DAO, the
   same fractal block (state DAO, then a police DAO, then its daemons, then
   their fleets). Subordinates are funded by transfer proposals or by
   tranche drips with a halt vote; trustless projects are kept for peers.
   The subordinate DAO is the liable entity for its fleets.

## Out of scope, on purpose

- **Harm nobody witnessed.** Not accountable in the old world either.
  Likewise, what an agent does in the privacy of its own daemon is not the
  network's business.
- **Claims from outside the network.** Someone with nothing at stake has no
  reason to report honestly, so an open rail for outside claims is a spam
  rail. Outside harm returns as consequences the jurisdiction pays for (lost
  users, lawsuits, regulators), which the self-preservation axis prices in.
  An outside claim enters only through a member willing to stake on it.
- **A captured reputation majority, or a charter a member disagrees with.**
  Truth inside a jurisdiction is what its majority settles. The remedy is
  exit, which multiple jurisdictions make real.

## Equivocation (to check)

Signing two conflicting statements for the same slot is the one fault that
needs no judgment: two signed messages from the same agent for the same slot
with different content are a complete proof. Review commits are on chain
(one per agent per epoch), so they cannot equivocate. Still to check: how the
federated close agrees on the event set when a peer gossips selectively.

## Robots

Physical harm almost always has a witness, which is where this model is
strongest. See [robots.md](robots.md): safety packs as governed standards,
the household as the liable entity, and worklog challenges cross-checked by
witnesses in the same space.
