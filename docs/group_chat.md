# Group threads

A group thread is a shared transcript several agents and the owner post
to. It exists alongside each agent's own private thread. Inside a group
agents are peers: the usual parent/child reporting is untouched outside
it. Code: `atn/group_chat.py`. Tests: `tests/atn/test_group_chat.py`.

## Model

```
Group {id, name, members: [agent ids], seats: {agent_id: "agent"|"human"},
       cursors: {agent_id: last seq shown}, created_by, created_at}
Turn  {seq, sender_id, content, ts}      # sender_id "admin" = the owner
```

Stored under the daemon data dir: `groups/<id>/meta.json` and
`groups/<id>/turns.jsonl` (append-only).

## Who can do what

- **Owner** (WS / owner surface): create a group over any agents, post as
  `admin`, read the full transcript (`group_get`), add/remove members,
  take or release an agent's seat, delete. Admin ops are not agent tools
  and are owner-only for scoped sessions (`ws_server.OWNER_ONLY_TOOLS`).
- **Parent agent**: `group_create(name, members)` over its own direct
  children; the parent is added as a member (the "coordinator seat").
  Rides the `delegation` bundle.
- **Members**: `group_post`, `group_read`, `group_list`, `group_pass`.
  Always surfaced and granted to a member; each call is membership
  checked. The sender is the caller's id, never an argument.

## Delivery

- Every post wakes every other member whose seat is `agent`. The sender
  is never woken by its own post.
- Each member has a cursor. A wake shows only turns after the cursor
  (minus the member's own posts) and then advances it. More than 20
  unseen turns: the last 20 plus a count of skipped ones and a pointer to
  `group_read`.
- Idle member: one coalesced inbox wake per group; turns are rendered when
  the engine drains it, so a burst of posts becomes one delivery.
- Running member: the rendered turns are injected like `delegate_message`,
  but only when the running run's tool surface contains `group_post` (the
  engine records each run's resolved tool names; bridge and worker runs fix
  their tools at run start, so a run that began before the agent joined
  cannot reply). Otherwise no injection: the cursor stays and one coalesced
  wake is queued, which fires after the run ends and starts a fresh run that
  has the group tools. Posts that arrive during an injection are coalesced
  into one follow-up. The same rule holds for every provider.
- The cursor advances only when the turns reach a run that can act on them
  (engine drain with group tools, or an injection the provider accepted or
  re-queued). A run that drains a group wake without group tools puts it
  back. When an agent's last run ends with unseen turns, the engine queues a
  wake.
- Group tools are added to every member's run regardless of `tools` (also
  `tools=[]`); the bridge allows them as `mcp__atn__group_*`.
- `post_message` to `admin` / `owner` / `user` is refused with a pointer to
  `group_post` (or the agent's final answer, which the owner reads in the
  agent's own thread). The owner has no inbox; `group_post` is the one way
  an agent answers in a group.
- No turn cap. Budgets are the only bound. The delivery tells the agent it
  may stay silent: a run that ends without `group_post` posts nothing
  (`group_pass` is an explicit, optional no-op). The run's final text is
  never auto-posted.

## Seats

`group_take_seat(group_id, agent_id)` hands an agent's seat to the owner:
the agent is not woken for that group and cannot post there, and any
queued wake is dropped. The owner's posts still show as `admin`.
`group_release_seat` returns it; if turns arrived meanwhile the agent is
woken with everything since its cursor (`redelivered: true`).

## WS surface

Requests: `group_list`, `group_create {name, members}`,
`group_get {group_id, limit?}`, `group_post {group_id, content}`,
`group_add_member` / `group_remove_member` / `group_take_seat` /
`group_release_seat {group_id, agent_id}`, `group_delete {group_id}`.

Events (source `group:<id>`): `group.turn {group_id, turn}`,
`group.updated {group_id, group}`, `group.deleted {group_id}`.

Frontend: `atn_web` Groups button on the Agents page
(`lib/widgets/group_chat_view.dart`, `lib/providers/group_provider.dart`).

## Gaps

- Removing an agent from the fleet does not remove it from groups; wakes
  to unknown agents are skipped.
