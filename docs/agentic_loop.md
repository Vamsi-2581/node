# Generic agentic loop: hardening spec (2026-07-04)

Binding spec for upgrading `atn/providers/base.py:run_agent_loop` (the
loop behind anthropic / openai_compat / ollama agents) and its adapters
to parity with current harnesses (Claude Code / Agent SDK, opencode,
Codex CLI). Grounded in: the 2026-07-04 live daemon session (findings
1-7 below), the loop audit, and the SOTA survey. The bridge (Claude
Agent SDK) keeps its own loop; only §10-11 touch it.

Live findings this spec answers:
1. Unknown model strings silently route to BridgeProvider
   (`provider_manager.py:483`): a `qwen3.5:4b` agent burned 45k
   subscription tokens on the bridge.
2. `create_agent` has no `provider` field; `update_agent` provider
   changes are inert (cached instance never evicted,
   `execution_engine.py:847`).
3. InputArbiter mic can be held by a dead WS surface forever; ghosts
   stay in `_connected` and recapture the mic on every hand-off.
   All input surfaces wedge with `input_not_active`.
4. Ollama requests never set `num_ctx`; ollama silently truncated a
   10,768-token request to 4096 (`server.log`) and the agent answered
   from hallucination. Non-bridge agents also get the full
   `_SHELL_TOOLS` schema block unconditionally (`execution_engine.py:909`).
5. Ollama multi-turn tool history is flattened to text
   (`_normalize_content`): the structured `tool_calls` / `role:"tool"`
   linkage is lost after turn 1.
6. A transient stream drop mid-loop aborts the whole agent loop
   (no loop-level retry); compaction failure silently no-ops into a
   guaranteed overflow.
7. Bridge + `claude-haiku-4-5` main-loop model = silent hot-spin
   (100% core, zero events, 20+ min); `kill_agent` reported success
   but left the spinning bun subprocess alive.
8. `delegate_message` mid-turn injection races turn completion: a
   message written to bridge stdin as the agent loop ends is
   accepted ("User message injected") and silently dropped: the
   daemon reports `injected` with no consumption ack and no inbox
   fallback. (Observed live: injection at 01:32:45.685, execution
   completed 01:32:45.)

## Design principles

- **Error classes are routes, not retries.** Transient → bounded
  resample. Overflow → context reduction, never retry. Fatal (auth,
  invalid request) → structured abort. (opencode `retry.ts` split.)
- **Two-tier context reduction: prune before compact.** Dropping old
  tool bodies is free; an LLM summary costs tokens and can fail.
- **History must always be replayable.** No orphaned `tool_use`
  without a `tool_result` on any exit path.
- **Steering parity.** `delegate_message` must land mid-turn on the
  generic loop, not just the bridge.
- **Fail loud on routing.** A model the daemon can't place is an
  error, not a silent fallback onto the user's subscription.

## §1 Loop-level retry gate (base.py)

Wrap the per-turn `send_stream` call:
- Classify: `overflow` (provider 400s whose message matches context /
  token-limit patterns; adapters map their provider's overflow shape
  to a shared `ContextOverflowError`), `transient` (timeouts, dropped
  streams, 5xx/429 that escaped adapter retries), `fatal` (everything
  else).
- Transient: up to 3 loop-level retries, exp backoff 2s/8s/30s +
  jitter; honor `Retry-After` when surfaced. Retry re-sends the same
  request (history unchanged).
- Overflow: jump to §2 reduction, then re-send once. Overflow after
  reduction → abort `context_overflow`.
- Fatal: abort with `stop_reason="provider_error"`, error text in the
  result, orphan repair per §3.

## §2 Context reduction: prune, then compact

Trigger **pre-send**: estimated next-request tokens (last response's
reported input + chars/4 of what was appended since, see §19) >
`ctx_window − max_tokens − 8k buffer`. `ctx_window` from
`model_specs.get_context_window` (see §7).

Tier 1, prune (no LLM call): replace `tool_result` bodies older than
the last 2 assistant turns with `[pruned: <n> chars, tool=<name>]`,
protecting the most recent 40k chars of tool output. Only prune when
it saves ≥ 20k estimated chars.

Tier 2, compact (only if still over): summarize everything except
(a) the original task message, (b) the last 2 turns, (c) the last
~20k chars of user messages, using the structured template: Goal /
Progress / Key decisions / Critical context / Next steps.
- On summarizer failure: **fallback hard truncation**, dropping oldest
  turns (keeping (a) through (c)), never no-op.
- **Spiral guard**: max 2 compactions per run; if a compaction reduces
  the estimate by <20%, abort `context_overflow` instead of looping.
  (Codex v0.112 / opencode #27924 class of bug.)

## §3 Orphan repair on every exit path

Any return/abort that leaves a trailing assistant message containing
`tool_use` blocks without matching results (budget_exceeded,
per_turn_input_exceeded, repeat abort, interrupt, provider_error)
appends synthetic `tool_result` blocks
(`{"cancelled": true, "reason": <abort reason>}`, `is_error=True`)
before returning, so persisted history replays cleanly against
Anthropic-shape APIs.

## §4 Loop detection

- Exact-repeat threshold drops 5 → 3 (opencode `DOOM_LOOP_THRESHOLD`).
- Oscillation guard: over the last 6 calls, if ≤ 2 distinct
  (tool, args-hash) fingerprints and every fingerprint appeared ≥ 2
  times → abort `loop_detected`.
- Before aborting, inject one warning turn: a user message telling the
  model it appears to be looping and must change approach or conclude;
  abort only if the next call repeats again. (The model gets one
  chance to self-correct; my own harness does the same.)

## §5 Mid-turn steering for the generic loop

- `Provider` base gains `send_user_message(content) -> bool` backed by
  an `asyncio.Queue` (`_steering_queue`). Returns True if an
  an agent loop is active to consume it.
- `run_agent_loop` drains the queue at each iteration boundary
  (after tool results are appended, before the next send) and appends
  each item as a `user` message.
- `execution_control.send_delegate_message` already prefers
  `provider.send_user_message`; with this, API-provider agents get
  live injection instead of the inbox fallback.
- **Delivery ack (finding 8, applies to bridge too):** injection is
  only "delivered" when consumed. Bridge: `claude-bridge.ts` emits
  `@@EVENT@@ user_message_consumed` when the SDK loop actually reads
  the injected message; `bridge.py` tracks pending injections and, on
  agent-loop end with unconsumed injections, re-posts them to the
  agent's inbox (HIGH WORK) so the next run picks them up. Generic
  loop: same contract, where steering-queue items not drained by run end
  are re-posted to the inbox. `delegate_message` result distinguishes
  `injected` (consumed or pending with fallback guarantee) so callers
  are never lied to.

## §6 Tool-result truncation: head + tail

Replace the tail-chop at `_TOOL_RESULT_CHAR_MAX` (40k) with head+tail:
first 24k + `\n…[elided <n> chars of <total>]…\n` + last 12k. Keeps
the command echo and the error tail (Codex shape). Cap unchanged.

## §7 Model metadata unification

- `get_context_window` / output caps come from `model_specs.py`
  everywhere; `base.py:CONTEXT_WINDOWS` becomes a thin re-export or is
  deleted. Per-request `max_tokens = min(16384, spec.max_output_tokens)`
  instead of the hardcoded 16384.
- Register local models in `model_specs.py`: `qwen3.5:4b`,
  `vibethinker-3b` (context 16384 default, `relative_cost=0`,
  tier: tool-use). Unknown ollama models default to 16384, not 128k.

## §8 Malformed tool arguments

When an adapter degrades unparseable tool-call args to `{"raw": ...}`,
the loop must NOT execute the tool. Return a synthetic error
`tool_result` ("arguments were not valid JSON; re-emit the call")
and continue the loop. Applies to all three adapters' parse paths.

## §9 Ollama adapter: structured history + num_ctx

- Translate canonical history natively: assistant `tool_use` blocks →
  ollama `message.tool_calls`; canonical `tool_result` blocks →
  `role:"tool"` messages (with `tool_name`). `_normalize_content`
  remains only for plain-text blocks. Round-trip test required:
  2-turn tool conversation re-sent to ollama preserves the linkage.
- Every request sets `options.num_ctx` = `get_context_window(model)`
  (per §7; 16384 default for local models). Never rely on ollama's
  4096 default.
- Lean tool surface: `execution_engine.py:909` stops appending
  `_SHELL_TOOLS` unconditionally for non-bridge providers; shell tools
  ride the existing `tools` categories (add a `"shell"` category) so
  a lean agent's prompt stays small.
- Web access is the same shape (2026-09-18): bridges have
  WebSearch/WebFetch natively under `"sdk_builtin"`; API and local
  providers get the daemon's `web_search`/`web_fetch`
  (`atn/web_tools.py`, keyless, DuckDuckGo HTML + httpx) only when the
  agent is granted `"web"`. Both are LOCAL tools like shell: the worker
  runs them in-process. No other bundle carries file or web access, and
  the common base prompt says so, so an agent without the grant asks
  instead of guessing that `atn_full` might.

## §10 Provider routing + lifecycle

- Routing is explicit (decision 2026-10-06): every cognitive agent names
  its `provider` AND `model`. There is no daemon-wide default, no
  inheritance from the parent and no routing by model prefix; an agent
  missing either raises `ProviderError` at resolve time. A per-agent
  `service_provider` binding (or provider `service`) names the model
  itself. Validation: `provider_identity.routing_error`.
- `create_agent` requires `provider` and `model` (cognitive mode);
  `update_agent` / `set_agent_model` can change them but never clear them.
- Agents written before the decision are pinned once at boot to what they
  already resolved to (`atn/legacy_routing_migration.py`), which also
  consumes and deletes the retired config.yaml `defaults:` section.
- `update_agent`: when `provider` or `model` changes, evict + close
  `_active_providers[agent_id]` (same cleanup as unregister_agent,
  minus file deletion) so the change is live.

## §11 Bridge: spin guard + kill ladder

- Root-cause the haiku hot-spin in `bridge/claude-bridge.ts` (SDK
  `query()` with a model that is not loop-capable). Whatever the mechanism:
  errors from `query()` must surface as an `@@EVENT@@ error` +
  request failure, never a bare loop continue.
- First-event watchdog: if a sent agent-loop request produces zero
  bridge events within 120s (configurable `ATN_BRIDGE_FIRST_EVENT_TIMEOUT`),
  kill the subprocess and fail the request; don't wait for the 1800s
  idle ceiling.
- Kill ladder: `kill_agent` → provider interrupt must escalate to
  subprocess termination (bun PID) within a 5s grace window, verified
  by the daemon (poll PID, `taskkill /F` fallback). A "killed"
  execution must never leave the provider subprocess running.
- Model-tier guard: reject agent-loop requests on models whose spec
  says `loop_capable=False` (haiku) with a clear error before
  spawning the SDK loop, since the SDK loop is known to misbehave.

## §12 InputArbiter liveness

- Ghost holder fix: `WebSocketBridge` validates the holder on every
  gate denial: if the holder token is a `ws:` surface with no live
  session, call `release_for(holder)` and re-run the gate. Cheap,
  contained, no arbiter API change.
- Hand-off successor selection skips `ws:` tokens with no live
  session (ws_server passes a liveness predicate to the arbiter at
  construction).
- `is_active` auto-acquire keeps `setdefault` but the predicate
  prevents dead tokens from re-entering via hand-off.

## §13 Usage self-awareness for agents (owner request 2026-07-04)

- New tool `get_usage` (category `budget`): returns the caller's
  provider usage: for bridge agents, the cached
  `refresh_usage` payload (5h/7d utilization + reset times) plus the
  agent's own budget consumption (`registry` budget counters); for API
  providers, cumulative session tokens vs configured budget. Cached
  ≤60s so agents polling it don't burn a probe call per turn.
- Purpose: agents can self-pace long-horizon work against the shared
  subscription the way the operator does, instead of running blind
  until a budget abort.

## §14 Local loop eligibility (owner request 2026-07-04)

- The "no cloud available" contingency: ollama models may serve as the
  root cognitive agent. Gate, don't hardcode: a model spec flag
  `loop_capable` (`atn/model_specs.py`) that local models can earn,
  rather than a blanket provider-level refusal on ollama.
- Prerequisites before flipping any local model on: §9 structured tool
  history (multi-turn tool use must round-trip), §7 num_ctx, and a
  smoke test (spawn child → collect → summarize) passing on that model.
- Fallback chains (`provider: [claude_max, ..., ollama]`) already
  exist; with this flag the chain can legally end in ollama so the
  fleet degrades to local instead of halting when cloud auth/quota is
  gone.

## §15 Manual compaction (owner request 2026-07-04)

New tool + WS message `compact_agent(agent_id)`.

Permissions: the owner (WS / OWNER_ID caller) may compact any
agent; an agent may compact only its **direct children**
(`target.parent_id == caller_id`); never itself. Violations return an
error, no side effects.

Semantics by target state:
- **Running, generic loop:** set a compact-requested flag on the
  provider; the loop honors it at the next iteration boundary by
  running `_reduce_context(force=True)`. Result `{"status": "queued"}`.
- **Running, bridge:** if the Claude Agent SDK input protocol supports
  a compact control message, inject it; otherwise return
  `{"status": "unsupported_while_running"}`. Do NOT pretend.
- **Idle (any provider):** compact the persisted conversation store:
  summarize all but the last 2 turns via the agent's own provider into
  one summary turn (same Goal/Progress/Decisions/Critical
  context/Next steps template), archive the original history
  (existing store archive mechanics), and evict the cached provider
  instance (bridge session_id dropped) so the next run rebuilds from
  the compacted store. Result `{"status": "compacted", "turns_before",
  "turns_after"}`.

Events: every manual compaction emits the existing CONTEXT_COMPACTION
event with `"manual": true` and `"requested_by": <caller_id>` in data,
statuses in_progress / completed / hard_truncated as today.

Frontend contract (atn_web): subscribe to `context.compaction` events:
live "Compacting…" indicator while in_progress, updated compaction chip
on completed, visually distinct warning on hard_truncated; a "Compact"
action (owner UI) sends `{"type": "compact_agent", "agent_id": ...}`.

## §17 Non-blocking shell tools (2026-10-12)

Ideas below §17 are ported from OpenAI Codex CLI (reimplemented, not
copied). Shell executors (`atn/shell_tools.py`) are coroutines awaited
on the daemon/worker event loop, so none may block it:
- `bash` and `search_files` run through `asyncio.create_subprocess_*`
  with stdout+stderr merged (interleaved in write order) and stdin
  closed (a prompting command fails fast instead of hanging).
- On timeout the whole process tree is killed (`taskkill /F /T` on
  Windows, `killpg` on POSIX, where the child gets its own session) and
  the partial output is returned with the error. Cancellation of the
  run also kills the tree.
- Output keeps head (30k) + tail (20k) chars, middle elided, instead
  of a head-only chop, so the final status and last traceback survive.
  Capture is bounded in memory (128 KiB head + 96 KiB rolling tail).
- `read_file` / `write_file` / `edit_file` / `list_directory` do their
  disk I/O in a worker thread (`asyncio.to_thread`).

## §18 Plain-text tool results

`format_tool_result` (base.py) renders what the model reads; it used to
`json.dumps` every result, wrapping shell output in quotes and escaped
newlines.
- string result: sent as-is.
- shell-shaped dict (`output` and/or `exit_code`, optional `error`):
  `Exit code: N` / `Error: ...` lines, then `Output:` and the raw text.
- dict with one text field (`content`, `matches`, `output`, `text`,
  `result`): that text. Dict with only `error`: `Error: ...`.
- anything else (genuinely structured): JSON, as before.
Always a string, so anthropic / openai_compat / ollama adapters pass it
through unchanged as tool content. §6 head+tail truncation applies after.

## §19 Context fullness from real usage

The per-turn ceiling and the §2 pre-send trigger no longer use chars/4
over the whole stack. `_next_input_estimate`: the provider-reported
input of the last response (`input + cache_read + cache_creation`,
which already counts system prompt and tool schemas) plus chars/4 of
the messages appended since that request. The anchor is per run and is
dropped whenever a reduction rewrites the stack (pre-send, reactive
overflow, manual compaction); without an anchor (first turn, usage of
0, or right after a reduction) the old chars/4 estimate is used.
`_reduce_context(est_tokens=...)` carries the gap between the real size
and chars/4 as a fixed offset on its char budget, so reduction aims at
the real size.

## §20 Cache-friendly compaction

The in-loop compactor (`_compact_messages` via `_summarize_for_compaction`)
no longer sends a separate summarizer system prompt (a guaranteed
prompt-cache miss on the run's largest request). The summary request is
the loop's own request: same system prompt, same tool schemas
(`_run_tool_defs`), the full current history as prefix, plus one final
user message (`COMPACTION_INLINE_PROMPT`: no tool calls, handoff note
under the Goal / Progress / Key decisions / Critical context / Next
steps headings). The result still becomes summary + retained tail (§2).
- If the summary request itself overflows, the oldest ~20% of history
  is dropped in valid units (an orphaned leading tool_result goes too; a
  user marker is prefixed if the rest starts on an assistant turn) and
  the request retried, up to 4 times, before falling to `_hard_truncate`.
- If the model answers with a tool call instead of text, retry once
  without tools.
- Spiral guards (§2) are unchanged. The idle-path `compact_agent` (§15)
  still uses `COMPACTION_SUMMARIZER_SYSTEM`; it has no live cache prefix.

## §21 Parallel read-only tools

When one turn carries several tool calls, the loop
(`_execute_tool_batches`) walks them in order: a maximal run of
consecutive parallel-safe calls executes concurrently (`asyncio.gather`,
at most 8 at once); any other call waits for the batch before it and
runs alone. That is read/write-lock semantics with fair, in-order
acquisition. Results are appended in the original call order.
- Parallel-safe = `PARALLEL_SAFE_TOOLS` (read_file, list_directory,
  search_files, web_fetch, web_search, probe_tools and the list/get
  framework tools that only read registries) plus any tool dict flagged
  `parallel_safe: True`; a tool dict may opt out with
  `parallel_safe: False`. bash, write/edit, use_tool, delegation and
  every mutating framework tool stay exclusive.
- Loop guard (§4) and malformed-args guard (§8) run as before; a call
  with malformed args is never part of a parallel batch and is not
  executed. An executor exception now also marks its result `is_error`.

## §22 Per-run plan tool (`update_plan`)

Every generic-loop run that has a tool surface and an executor gets
`update_plan` (`UPDATE_PLAN_TOOL`): `{plan: [{step, status}],
explanation?}`, status one of pending / in_progress / completed, at most
one in_progress, max 50 steps. Each call replaces the whole plan.
- Handled by the loop, never by `tool_executor`: validated, stored in
  run state (`_run_plan`, `_run_plan_explanation`, reset per run),
  answered `Plan updated.` or an `is_error` result. A `STEP_OUTPUT`
  event with `channel: "plan"` carries the plan to the UI.
- Survives context reduction: `format_plan` text is inserted verbatim
  into the compaction summary message, the `_hard_truncate` head and the
  tier-3 hard-reset notice (not left to the summarizer).
- Bridge providers keep their own SDK todo tooling; unaffected.

## §23 Context headroom signal

After a turn's tool results are built, the loop computes the next
request size (§19 estimate) against the usable input budget
(`compaction_trigger_tokens`). On first crossing 70%, 80% and 90%, a
short note (percent in use, tokens left before older history is
summarized, advice to keep output small) is appended to the LAST tool
result of that turn. Never in the system prompt, which must stay
byte-stable for the prompt cache. Each level is announced once per
run; falling back under 70% (after a reduction) re-arms them.

## §24 History hygiene

- **Whole-stack pairing before every send** (`_repair_history`, run at
  the top of each `_send_stream_with_retry` attempt, so also after a
  reactive reduction). A tool_use with no result in the next message
  gets a synthetic `is_error` "no result was recorded" result (merged
  into the next tool-result message, results first, or a new user
  message inserted). A tool_result whose id is not a tool_use of the
  immediately preceding assistant turn (e.g. a compaction tail that
  starts on results whose call was summarized away) becomes a short
  text block. Valid stacks are never touched (cache prefix intact). The
  trailing assistant turn is left to the loop / §3 exit repair.
- **Output cap mid tool call.** `stop_reason == "max_tokens"` with tool
  calls means the last call is cut off. The turn is discarded (any text
  kept as an assistant message, the partial calls never enter history)
  and one user message (`TRUNCATED_TOOL_CALL_PROMPT`) asks for a
  smaller re-issue. Once per incident: a second consecutive truncation
  finalizes as before; a normal tool turn re-arms it.
- **Backoff.** Connection-level failures (refused / reset / dropped
  stream, also when wrapped as `__cause__` of a ProviderError without a
  status) route as transient on their own schedule, 1/2/4/8/16s +
  jitter, separate from the 2/8/30s 429/5xx schedule. `Retry-After` is
  honored up to 300s.

## §25 Loop policy comes from the agent's distro (2026-10-12)

Every policy in this document that is a CHOICE rather than a safety
property is now a field of `HarnessPolicy` (`atn/harness_policy.py`),
carried in the harness distro's config blob (docs/tool_substrate.md,
Decision 2026-10-12). The loop reads it per run:

- **Prompts:** common base, per-category tool notes, type guidance,
  small-context note (`delegate_prompts.build_system_prompt(...,
  policy=)` / `build_identity_header(..., policy=)`).
- **Closing turns:** verify on/off, verify prompt, code-file suffixes;
  review on/off, review prompt; bridge follow-up turn caps (8 / 4).
- **Compaction (§2, §20):** inline instruction, prune protect/min
  savings, retained turns + user chars, max compactions, min reduction,
  summary max tokens, overflow retries + drop fraction, hard-reset cap,
  reset digest cap.
- **Guards (§4, §24):** repeat limit, oscillation window, per-turn input
  ceiling, loop warning text, truncated-tool-call prompt.
- **Headroom (§23):** levels and note template. **Parallel (§21):**
  safe tool list and concurrency cap. **Tool bundles:** an optional
  list that NARROWS the agent's granted categories (never widens).

Resolution: agent definition knobs (`per_turn_input_max`,
`repeat_call_limit`) > the agent's bound distro (`AgentDefinition.
loadout`, owner-set) > the reference policy. The reference policy is
built from the module constants, so an agent with no loadout runs a
byte-identical loop (`tests/atn/test_swappable_harness.py`). A distro
policy is an overlay: missing keys inherit, unknown keys are ignored,
bad values (wrong type, template missing its placeholders) keep the
reference value, numbers are clamped. In-process runs pass the policy
object; worker runs carry it as a dict in the run manifest (only when
it differs from the reference).

NOT policy (kernel, never distro-settable): the budget gate and usage
recorder, tool dispatch + containment, the constitutional preamble, the
governance heartbeat, history storage, `_repair_history`, `update_plan`
validation. Policy-as-code hooks (`capabilities.provides:
["policy:compaction"]`, etc.) are declarable but not dispatched yet;
see `POLICY_HOOKS` in `atn/harness_policy.py` for the intended contract.

## Not ported: AGENTS.md

Codex loads AGENTS.md from repo root down to the working directory.
Not ported: cognitive agents on the generic loop have no working
directory of their own (shell tools run in the daemon's process cwd;
`agents_dir/<id>` work dirs exist only for pipeline agents). Revisit if
a per-agent workdir is introduced.

## Testing (targeted only, never the full suite)

New/updated files: `tests/atn/test_loop_hardening.py` (§1-§6, §8:
mock provider raising transient/overflow/fatal; compaction fallback;
orphan repair; oscillation; steering queue), extend
`tests/atn/test_provider_base.py` (head+tail truncation, max_tokens
from spec), `tests/atn/test_ollama_translation.py` (§9 round-trip,
num_ctx presence), `tests/atn/test_provider_routing.py` (§10 fail-loud
+ eviction), `tests/atn/test_input_arbiter.py` (§12 ghost recovery).
Codex-port items §17-§24: `tests/atn/test_loop_codex_port.py`.
§25 (distro policy, loadouts, core swaps): `tests/atn/test_swappable_harness.py`.
Run alongside existing `test_provider_base.py`,
`test_long_horizon_safeguards.py`, `test_budget_inner_loop.py`.

Out of scope: worker-isolation default, voice/chat surfaces, sponsor
paths, any consensus/substrate code.
