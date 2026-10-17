/**
 * claude-bridge.ts -- stdin/stdout bridge between ATN and Claude Agent SDK.
 *
 * Protocol (NDJSON over stdin/stdout):
 *   stdin  (ATN -> bridge): JSON requests + tool_result messages
 *   stdout (bridge -> ATN): JSON responses + tool_call messages
 *   stderr: debug logging + @@EVENT@@ streaming events
 *
 * Request types:
 *   create      — single-turn LLM call (maxTurns=1, no tools)
 *   agent loop — multi-turn with ATN tools as in-process MCP server
 *   delete      — clean up a session
 *   shutdown    — graceful exit
 */

import { query, tool, createSdkMcpServer, getSessionMessages } from "@anthropic-ai/claude-agent-sdk"
import type { Query, SDKMessage, SDKUserMessage, Options } from "@anthropic-ai/claude-agent-sdk"
import { z } from "zod"
import { randomUUID } from "crypto"
import { execSync } from "child_process"
import { existsSync, readFileSync, appendFileSync, mkdirSync } from "fs"
import { fileURLToPath } from "url"
import { join, dirname } from "path"
import { createInterface } from "readline"

// -- Strip nesting guard --
// The SDK spawns a Claude Code subprocess that refuses to start if
// CLAUDECODE is set.  Clear it so the bridge can operate normally.
delete process.env.CLAUDECODE

// -- Logging (stderr only) --

function log(msg: string, extra?: Record<string, unknown>): void {
  const parts = ["[bridge]", msg]
  if (extra && Object.keys(extra).length > 0) {
    parts.push(JSON.stringify(extra))
  }
  process.stderr.write(parts.join(" ") + "\n")
}

// -- Effort pinning --
// ATN_BRIDGE_EFFORT pins the SDK reasoning-effort level (low|medium|high|
// xhigh|max) for every query this bridge issues. Unset = SDK default (high).
const pinnedEffort = process.env.ATN_BRIDGE_EFFORT || ""

// -- Turn-level trace capture --
// ATN_BRIDGE_TRACE_DIR, when set, appends every SDK stream message (assistant
// turns, tool results, system events) as JSONL to <dir>/<session_id>.jsonl.
// This is the raw per-turn trajectory record that execution summaries lack.
const traceDir = process.env.ATN_BRIDGE_TRACE_DIR || ""
if (traceDir) {
  try { mkdirSync(traceDir, { recursive: true }) } catch { /* best-effort */ }
}

function trace(message: unknown): void {
  if (!traceDir) return
  try {
    const sid = (message as any)?.session_id || "unknown"
    appendFileSync(join(traceDir, `${sid}.jsonl`), JSON.stringify(message) + "\n")
  } catch { /* tracing must never break the run */ }
}

// -- Streaming events (@@EVENT@@ on stderr) --

const EVENT_PREFIX = "@@EVENT@@"

function emitEvent(event: { type: string; [key: string]: unknown }): void {
  process.stderr.write(`${EVENT_PREFIX}${JSON.stringify(event)}\n`)
}

// -- Rate-limit snapshot --
// The Claude Agent SDK emits SDKRateLimitEvent stream messages carrying the
// subscription-quota data rendered by Claude Code's /usage command.  We keep
// one entry per rateLimitType (five_hour, seven_day, seven_day_sonnet, etc.)
// and forward each update to the Python side as @@EVENT@@ rate_limit.

type RateLimitEntry = {
  status: string
  rateLimitType: string
  utilization?: number
  resetsAt?: number
  overageStatus?: string
  overageResetsAt?: number
  overageDisabledReason?: string
  isUsingOverage?: boolean
  surpassedThreshold?: number
  updatedAt: number
}

const rateLimitSnapshot: Record<string, RateLimitEntry> = {}

function handleRateLimitEvent(msg: any): void {
  const info = msg?.rate_limit_info
  if (!info || typeof info !== "object") return
  const key: string = info.rateLimitType || "unknown"
  // Most rate_limit events carry utilization=null; only some include the
  // number. Preserve the last-known value so the cache stays meaningful —
  // a null must never erase a real reading.
  const prevUtil = rateLimitSnapshot[key]?.utilization
  const entry: RateLimitEntry = {
    status: info.status,
    rateLimitType: key,
    utilization: info.utilization ?? prevUtil,
    resetsAt: info.resetsAt,
    overageStatus: info.overageStatus,
    overageResetsAt: info.overageResetsAt,
    overageDisabledReason: info.overageDisabledReason,
    isUsingOverage: info.isUsingOverage,
    surpassedThreshold: info.surpassedThreshold,
    updatedAt: Date.now(),
  }
  rateLimitSnapshot[key] = entry
  emitEvent({ type: "rate_limit", ...entry })
  log("rate_limit", { type: key, utilization: info.utilization, status: info.status })
}


// -- Subscription usage (get_usage) --
// The sanctioned source for plan utilization is the SDK's get_usage control
// request (the data behind Claude Code's /usage). It is sent on a throwaway
// query whose prompt iterable never yields, so no model call is made; the
// CLI reads its own credentials. The method is experimental and its name
// will change, so it is feature-detected: a missing method yields
// available:false, never a crash and never a fallback to a direct API call.

const USAGE_TIMEOUT_MS = 30_000

function findUsageMethod(q: any): ((opts: any) => Promise<any>) | null {
  const known = "usage_EXPERIMENTAL_MAY_CHANGE_DO_NOT_RELY_ON_THIS_API_YET"
  if (typeof q?.[known] === "function") return q[known].bind(q)
  for (const name of ["usage", "getUsage"]) {
    if (typeof q?.[name] === "function") return q[name].bind(q)
  }
  // Renamed experimental method: anything shaped usage_EXPERIMENTAL*.
  let proto = q
  while (proto && proto !== Object.prototype) {
    for (const name of Object.getOwnPropertyNames(proto)) {
      if (/^usage_?experimental/i.test(name) && typeof q[name] === "function") return q[name].bind(q)
    }
    proto = Object.getPrototypeOf(proto)
  }
  return null
}

function isoToEpochSeconds(iso: unknown): number | undefined {
  if (typeof iso !== "string" || !iso) return undefined
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : undefined
}

// Map the get_usage reply onto the RateLimitEntry shape the rate_limit
// stream events already use (utilization 0-1, resetsAt epoch seconds).
function mapUsageWindows(rateLimits: Record<string, any> | null | undefined): Record<string, RateLimitEntry> {
  const out: Record<string, RateLimitEntry> = {}
  if (!rateLimits || typeof rateLimits !== "object") return out
  const now = Date.now()
  for (const [key, win] of Object.entries(rateLimits)) {
    // Only the documented plan windows with a real reading; unnamed or
    // codenamed entries (and extra_usage, which is spend, not a window)
    // would render as stray tiles on the provider card.
    if (!/^(five_hour|seven_day)/.test(key)) continue
    if (!win || typeof win !== "object" || typeof win.utilization !== "number") continue
    const util = win.utilization / 100
    out[key] = {
      status: util >= 1 ? "rejected" : "allowed",
      rateLimitType: key,
      utilization: util,
      resetsAt: isoToEpochSeconds(win.resets_at),
      updatedAt: now,
    }
  }
  return out
}

async function fetchUsage(): Promise<BridgeResponse & { id: string }> {
  const idle: AsyncIterable<SDKUserMessage> = {
    [Symbol.asyncIterator]: () => ({ next: () => new Promise<IteratorResult<SDKUserMessage>>(() => {}) }),
  }
  const q = query({
    prompt: idle,
    options: {
      pathToClaudeCodeExecutable: claudeExecutable,
      tools: [],
      settingSources: [],
      strictMcpConfig: true,
      persistSession: false,
    } as Options,
  })
  try {
    const fn = findUsageMethod(q)
    if (!fn) return { id: "", ok: true, available: false, reason: "sdk has no usage method" }
    const resp: any = await Promise.race([
      fn({ skipBehaviors: true }),
      new Promise((_, rej) => setTimeout(() => rej(new Error("get_usage timed out")), USAGE_TIMEOUT_MS)),
    ])
    if (!resp?.rate_limits_available || !resp?.rate_limits) {
      return { id: "", ok: true, available: false, reason: "plan rate limits unavailable",
               subscription_type: resp?.subscription_type ?? null }
    }
    const windows = mapUsageWindows(resp.rate_limits)
    for (const [key, entry] of Object.entries(windows)) {
      const prev = rateLimitSnapshot[key]
      rateLimitSnapshot[key] = { ...(prev || {}), ...entry }
    }
    return { id: "", ok: true, available: true, subscription_type: resp.subscription_type ?? null,
             rate_limits: windows }
  } finally {
    try { q.close() } catch {}
  }
}

// -- Global error handlers --
//
// A swallowed error is the root of the haiku hot-spin: the SDK's query()
// loop can reject in a background promise (e.g. an invalid/rejected model
// like claude-haiku-4-5 on the agent loop path). The old handlers just
// logged and kept the process alive, so the running agent-loop run never got
// a `result` message, never called respond(), and the SDK kept retrying
// internally at 100% CPU with zero @@EVENT@@ output. If an agent-loop run is
// active when an unhandled error fires, we now FAIL that agent-loop run
// loudly (error event + ok:false response + query.close) instead of leaving
// it to spin. When nothing is active, we keep the old keep-alive behaviour.

function failActiveAgentLoop(reason: string): boolean {
  if (!activeAgentLoop) return false
  const ao = activeAgentLoop
  activeAgentLoop = null
  log("failing active agent-loop run due to unhandled error", { reason })
  emitEvent({ type: "error", error: reason })
  try { ao.inputQueue.done() } catch {}
  try { ao.query.close() } catch {}
  const cb = ao.onFatal
  if (cb) {
    try { cb(new Error(reason)) } catch {}
  }
  return true
}

process.on("unhandledRejection", (reason: unknown) => {
  const msg = reason instanceof Error ? reason.message : String(reason)
  if (failActiveAgentLoop(`unhandled rejection: ${msg}`)) return
  log("unhandled rejection (kept alive)", { error: msg })
})

process.on("uncaughtException", (err: Error) => {
  if (failActiveAgentLoop(`uncaught exception: ${err.message}`)) return
  log("uncaught exception (kept alive)", { error: err.message })
})

// -- Claude executable resolution --

function resolveClaudeExecutable(): string {
  // 1. SDK's bundled cli.js (legacy SDK <0.2.119 bundled the CLI directly).
  try {
    const sdkPath = fileURLToPath(import.meta.resolve("@anthropic-ai/claude-agent-sdk"))
    const sdkCliJs = join(dirname(sdkPath), "cli.js")
    if (existsSync(sdkCliJs)) return sdkCliJs
  } catch {}

  // 2. The standalone @anthropic-ai/claude-code package, native binary.
  //    Resolve via Node's module resolver and look up its bin entry. This
  //    is more reliable than `where claude` on Windows, where the shell
  //    shim has no extension and Node cannot spawn it directly.
  try {
    const ccPkgJsonPath = fileURLToPath(import.meta.resolve("@anthropic-ai/claude-code/package.json"))
    const ccDir = dirname(ccPkgJsonPath)
    const pkgJson = JSON.parse(readFileSync(ccPkgJsonPath, "utf-8"))
    const binEntry = typeof pkgJson.bin === "string" ? pkgJson.bin : pkgJson.bin?.claude
    if (binEntry) {
      const binPath = join(ccDir, binEntry)
      if (existsSync(binPath)) return binPath
    }
  } catch {}

  // 3. System-installed claude on PATH. On Windows prefer the .cmd shim
  //    over the bash shim so Node's child_process knows how to spawn it.
  try {
    const cmd = process.platform === "win32" ? "where claude" : "which claude"
    const out = execSync(cmd, { encoding: "utf-8" }).trim()
    const candidates = out.split("\n").map(s => s.trim()).filter(Boolean)
    if (process.platform === "win32") {
      const cmdShim = candidates.find(p => p.toLowerCase().endsWith(".cmd"))
      if (cmdShim && existsSync(cmdShim)) return cmdShim
    }
    if (candidates[0] && existsSync(candidates[0])) return candidates[0]
  } catch {}

  throw new Error("Could not find Claude Code executable. Install via: npm install -g @anthropic-ai/claude-code")
}

const claudeExecutable = resolveClaudeExecutable()
log("resolved claude executable", { path: claudeExecutable })

function mapModelToClaudeModel(model: string | undefined): string {
  // Accept full model IDs (e.g. "claude-sonnet-4-20250514", "claude-fable-5")
  // or family names (e.g. "sonnet", "opus", "fable").  Full IDs are passed
  // through as-is — the Agent SDK resolves whatever string we hand it, so new
  // model IDs never need a mapping entry here.  Family names map to the SDK's
  // short aliases.  There is no default model (decision 2026-10-06): a missing
  // or non-Claude id is a loud error, never a silent remap onto sonnet.
  if (!model) {
    throw new Error(
      "No model specified: the agent must name its Claude model explicitly (no default model)",
    )
  }
  if (model.startsWith("claude-")) return model
  const m = model.toLowerCase()
  if (m.includes("fable")) return "fable"
  if (m.includes("mythos")) return "mythos"
  if (m.includes("opus")) return "opus"
  if (m.includes("haiku")) return "haiku"
  if (m.includes("sonnet")) return "sonnet"
  throw new Error(
    `Model '${model}' is not a Claude model: the claude_max bridge serves Claude models only`,
  )
}

// Capability rules, version-aware so new point releases in a family work with
// no edit here.  Returns true when the model carries a 1M-token context window
// (and therefore needs the context-1m beta on the agent loop path).
function modelHasLargeContext(model: string): boolean {
  const m = (model || "").toLowerCase()
  // Fable/Mythos 5 and all Sonnet 4.x are 1M.
  if (m.includes("fable") || m.includes("mythos")) return true
  if (m.includes("sonnet")) return true
  // Opus is 1M from 4.7 onward (4.6 and earlier are 200k).  Match any
  // opus-4-<minor> with minor >= 7, or opus-<major>-* with major >= 5.
  const opusMinor = m.match(/opus-4-(\d+)/)
  if (opusMinor && Number(opusMinor[1]) >= 7) return true
  const opusMajor = m.match(/opus-(\d+)/)
  if (opusMajor && Number(opusMajor[1]) >= 5) return true
  return false
}

// -- AsyncQueue --

class AsyncQueue<T> implements AsyncIterable<T> {
  private queue: T[] = []
  private resolve: ((value: IteratorResult<T>) => void) | null = null
  private finished = false

  push(item: T): void {
    if (this.finished) throw new Error("Queue is closed")
    if (this.resolve) {
      const r = this.resolve
      this.resolve = null
      r({ value: item, done: false })
    } else {
      this.queue.push(item)
    }
  }

  done(): void {
    this.finished = true
    if (this.resolve) {
      const r = this.resolve
      this.resolve = null
      r({ value: undefined as any, done: true })
    }
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return {
      next: (): Promise<IteratorResult<T>> => {
        if (this.queue.length > 0) {
          return Promise.resolve({ value: this.queue.shift()!, done: false })
        }
        if (this.finished) {
          return Promise.resolve({ value: undefined as any, done: true })
        }
        return new Promise((resolve) => {
          this.resolve = resolve
        })
      },
    }
  }
}

// -- Tool relay --
// During an agent loop request, ATN tool calls are relayed to Python for
// execution.  The bridge sends tool_call on stdout, Python responds with
// tool_result on stdin.  Pending calls are tracked by call_id.

const pendingToolCalls = new Map<string, {
  resolve: (result: any) => void
  reject: (error: Error) => void
}>()

function relayToolCall(name: string, input: unknown): Promise<any> {
  const callId = randomUUID().slice(0, 12)
  emitEvent({ type: "tool_use_start", tool_use_id: callId, tool_name: name, input })
  return new Promise((resolve, reject) => {
    pendingToolCalls.set(callId, { resolve, reject })
    const msg = JSON.stringify({
      type: "tool_call",
      call_id: callId,
      name,
      input,
    })
    process.stdout.write(msg + "\n")
    log("tool_call relayed", { callId, name })
  })
}

function handleToolResult(callId: string, result: any): void {
  const pending = pendingToolCalls.get(callId)
  if (pending) {
    pendingToolCalls.delete(callId)
    pending.resolve(result)
    const isError = result != null && typeof result === "object" && !!result.error
    emitEvent({ type: "tool_use_result", tool_use_id: callId, is_error: isError })
    log("tool_result received", { callId })
  } else {
    log("tool_result for unknown call_id", { callId })
  }
}

// -- JSON Schema to Zod conversion --
// The SDK tool() requires Zod schemas.  ATN tools use JSON Schema.
// Convert at runtime.

function jsonSchemaToZod(schema: Record<string, any>): z.ZodType {
  if (!schema || !schema.properties) {
    return z.object({})
  }

  const shape: Record<string, z.ZodType> = {}
  const required = new Set(schema.required || [])

  for (const [key, prop] of Object.entries<any>(schema.properties)) {
    let field: z.ZodType

    switch (prop.type) {
      case "string":
        field = prop.enum
          ? z.enum(prop.enum as [string, ...string[]])
          : z.string()
        break
      case "number":
      case "integer":
        field = z.number()
        break
      case "boolean":
        field = z.boolean()
        break
      case "array":
        field = z.array(prop.items ? jsonSchemaToZod(prop.items) : z.any())
        break
      case "object":
        // If no properties defined, allow arbitrary keys (passthrough)
        if (!prop.properties) {
          field = z.record(z.string(), z.any())
        } else {
          field = jsonSchemaToZod(prop)
        }
        break
      default:
        field = z.any()
    }

    if (prop.description) {
      field = (field as any).describe(prop.description)
    }

    if (!required.has(key)) {
      field = field.optional() as any
    }

    shape[key] = field
  }

  return z.object(shape)
}

// -- Build in-process MCP server from ATN tool definitions --

interface ATNToolDef {
  name: string
  description: string
  input_schema: Record<string, any>
}

function buildATNMcpServer(toolDefs: ATNToolDef[]) {
  const tools = toolDefs.map((td) => {
    // Convert JSON Schema to Zod shape for the SDK
    const zodSchema = jsonSchemaToZod(td.input_schema)

    // The SDK tool() wants a raw shape object, not a ZodObject.
    // Extract the shape if it's a ZodObject, otherwise wrap in passthrough.
    let schemaArg: any
    if (zodSchema instanceof z.ZodObject) {
      schemaArg = zodSchema.shape
    } else {
      schemaArg = { _input: z.any().describe("Tool input") }
    }

    return tool(
      td.name,
      td.description,
      schemaArg,
      async (args: any) => {
        // Relay the tool call to Python and wait for the result
        const result = await relayToolCall(td.name, args)
        return {
          content: [{
            type: "text" as const,
            text: typeof result === "string" ? result : JSON.stringify(result),
          }],
        }
      },
    )
  })

  return createSdkMcpServer({
    name: "atn",
    version: "1.0.0",
    tools,
  })
}

// -- Session types --

interface BridgeSession {
  id: string
  query: Query
  iterator: AsyncIterator<SDKMessage, void>
  inputQueue: AsyncQueue<SDKUserMessage>
  resolvedModel: string | null
  createdAt: number
}

// -- Session Manager --

class SessionManager {
  private sessions = new Map<string, BridgeSession>()

  createAndSend(
    firstMessage: string,
    systemPrompt: string,
    options: Options,
  ): BridgeSession {
    const id = randomUUID()
    const inputQueue = new AsyncQueue<SDKUserMessage>()

    let content = ""
    if (systemPrompt) {
      content += systemPrompt + "\n\n"
    }
    content += `Human: ${firstMessage}`

    const firstUserMessage: SDKUserMessage = {
      type: "user",
      message: { role: "user", content },
      parent_tool_use_id: null,
      session_id: "",
    }

    inputQueue.push(firstUserMessage)

    const q = query({
      prompt: inputQueue,
      options,
    })

    const iterator = q[Symbol.asyncIterator]()

    const session: BridgeSession = {
      id,
      query: q,
      iterator,
      inputQueue,
      resolvedModel: null,
      createdAt: Date.now(),
    }

    this.sessions.set(id, session)
    log("session.created", { sessionId: id })
    return session
  }

  async collectResponse(session: BridgeSession): Promise<{
    text: string
    thinking: string[]
    toolCalls: Array<{ id: string; name: string; input: unknown }>
    usage: {
      input_tokens: number
      output_tokens: number
      cache_read_input_tokens: number
      cache_creation_input_tokens: number
    }
    model: string | null
    stopReason: string
  }> {
    let text = ""
    const thinking: string[] = []
    const toolCalls: Array<{ id: string; name: string; input: unknown }> = []
    let stopReason = "end_turn"

    try {
      while (true) {
        let message: SDKMessage | undefined
        try {
          const { value, done } = await session.iterator.next()
          if (done || !value) break
          message = value
        } catch (iterErr) {
          log("session.iterator.next() threw", {
            sessionId: session.id,
            error: iterErr instanceof Error ? iterErr.message : String(iterErr),
          })
          if (text) {
            text += "\n\n[Bridge: SDK error during execution - partial response returned]"
          }
          break
        }

        log("session.message", {
          sessionId: session.id,
          type: message.type,
          subtype: (message as any).subtype,
        })

        // Capture resolved model from SDK init message
        if (message.type === "system" && (message as any).subtype === "init") {
          const initModel = (message as any).model
          if (initModel && typeof initModel === "string") {
            session.resolvedModel = initModel
            log("session.resolvedModel", { sessionId: session.id, model: initModel })
          }
        }

        // Subscription-quota updates (Claude Max)
        if (message.type === "rate_limit_event") {
          handleRateLimitEvent(message)
        }

        if (message.type === "assistant") {
          // Capture model from underlying API response
          if (!session.resolvedModel && (message as any).message?.model) {
            session.resolvedModel = (message as any).message.model
          }
          for (const block of message.message.content) {
            if (block.type === "text") {
              text += block.text
              if (block.text) {
                emitEvent({ type: "text_delta", text: block.text })
              }
            } else if (block.type === "thinking") {
              const thinkText = (block as any).thinking || ""
              if (thinkText) {
                thinking.push(thinkText)
                emitEvent({ type: "thinking", text: thinkText })
              }
            } else if (block.type === "tool_use") {
              toolCalls.push({
                id: block.id,
                name: block.name,
                input: block.input,
              })
            }
          }
        }

        if (message.type === "result") {
          const resultMsg = message as any

          // Extract usage from modelUsage
          let inputTokens = 0
          let outputTokens = 0
          let cacheReadTokens = 0
          let cacheCreationTokens = 0
          const modelUsage = resultMsg.modelUsage as Record<string, any> | undefined
          if (modelUsage) {
            for (const mu of Object.values(modelUsage)) {
              if (mu && typeof mu === "object") {
                inputTokens += mu.inputTokens ?? 0
                outputTokens += mu.outputTokens ?? 0
                cacheReadTokens += mu.cacheReadInputTokens ?? 0
                cacheCreationTokens += mu.cacheCreationInputTokens ?? 0
              }
            }
          }

          log("session.result", {
            sessionId: session.id,
            subtype: resultMsg.subtype,
            textLen: text.length,
            thinkingBlocks: thinking.length,
            toolCalls: toolCalls.length,
            usage: { inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens },
          })

          emitEvent({ type: "done" })

          if (toolCalls.length > 0) {
            stopReason = "tool_use"
          }

          return {
            text,
            thinking,
            toolCalls,
            usage: {
              input_tokens: inputTokens,
              output_tokens: outputTokens,
              cache_read_input_tokens: cacheReadTokens,
              cache_creation_input_tokens: cacheCreationTokens,
            },
            model: session.resolvedModel,
            stopReason,
          }
        }
      }
    } catch (err) {
      log("collectResponse error", {
        error: err instanceof Error ? err.message : String(err),
      })
    }

    return {
      text,
      thinking,
      toolCalls,
      usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
      model: session.resolvedModel,
      stopReason,
    }
  }

  delete(id: string): boolean {
    const session = this.sessions.get(id)
    if (!session) return false

    try {
      session.inputQueue.done()
      session.query.close()
    } catch {
      // Already closed
    }
    this.sessions.delete(id)
    log("session.deleted", { sessionId: id })
    return true
  }

  destroy(): void {
    for (const id of [...this.sessions.keys()]) {
      this.delete(id)
    }
  }
}

// -- Protocol types --

interface CreateRequest {
  id: string
  type: "create"
  message: string
  system?: string
  model?: string
}

interface AgentLoopRequest {
  id: string
  type: "agent_loop"
  message: string
  system?: string
  system_prompt?: string   // Passed as SDK systemPrompt option (cached)
  model?: string
  tools: ATNToolDef[]
  max_turns?: number
  session_id?: string      // Resume this SDK session (loads history)
  native_tools?: boolean   // If true, allow all native Claude tools (Bash, Read, Write, etc.)
  // An imported Claude Code session: run it as the user's own Claude Code
  // would, in its folder, with the project's settings and Claude Code's
  // system prompt and tools. ATN's tools ride along as the atn MCP server.
  claude_session?: { folder: string; session_id: string }
}

interface DeleteRequest {
  id: string
  type: "delete"
  session_id: string
}

interface ShutdownRequest {
  id: string
  type: "shutdown"
}

interface PingRequest {
  id: string
  type: "ping"
}

interface GetUsageRequest {
  id: string
  type: "get_usage"
}

interface ToolResultMessage {
  type: "tool_result"
  call_id: string
  result: any
}

interface InterruptMessage {
  type: "interrupt"
}

interface UserMessageMessage {
  type: "user_message"
  content: string
}

interface SessionContextRequest {
  id: string
  type: "session_context"
  session_id: string
}

type BridgeRequest = CreateRequest | AgentLoopRequest | DeleteRequest | ShutdownRequest | PingRequest | SessionContextRequest | GetUsageRequest

interface BridgeResponse {
  id: string
  ok: boolean
  session_id?: string
  text?: string
  thinking?: string[]
  tool_calls?: Array<{ id: string; name: string; input: unknown }>
  stop_reason?: string
  usage?: {
    input_tokens: number
    output_tokens: number
    cache_read_input_tokens: number
    cache_creation_input_tokens: number
  }
  context?: {
    num_turns: number
    total_cost_usd: number
    context_window: number
    max_output_tokens: number
  }
  model?: string
  error?: string
  // session_context response
  messages?: Array<{ role: string; content: string }>
  stats?: Record<string, unknown>
  // get_usage response
  available?: boolean
  reason?: string
  subscription_type?: string | null
  rate_limits?: Record<string, RateLimitEntry>
}

// -- Response writer --

function respond(resp: BridgeResponse): void {
  process.stdout.write(JSON.stringify(resp) + "\n")
}

// -- Main --

const sessionManager = new SessionManager()

// -- Background tasks (shells, subagents, monitors) --
// The SDK reports every live background task (background_tasks_changed,
// REPLACE semantics) plus a task_notification when one ends. We mirror them
// to the Python side as @@EVENT@@ background_tasks so the daemon can show an
// agent's running shells, read their output and stop them. A run whose
// agent answered while tasks are still live keeps the session open: the SDK
// wakes the agent when a task finishes (Decision 2026-10-16, option A).

type BgTask = {
  task_id: string
  task_type: string
  description: string
  status: string            // running | completed | failed | stopped
  started_at: number
  ended_at?: number
  output_file?: string
  tool_use_id?: string
}

class BgTaskTracker {
  tasks = new Map<string, BgTask>()

  live(): BgTask[] {
    return [...this.tasks.values()].filter(t => t.status === "running")
  }

  private emit(): void {
    emitEvent({ type: "background_tasks", tasks: [...this.tasks.values()] })
  }

  handle(message: any): void {
    if (message?.type === "system" && message.subtype === "background_tasks_changed") {
      const liveIds = new Set<string>()
      for (const t of message.tasks || []) {
        if (!t?.task_id || t.ambient) continue
        liveIds.add(t.task_id)
        const prev = this.tasks.get(t.task_id)
        this.tasks.set(t.task_id, {
          started_at: prev?.started_at ?? Date.now(),
          output_file: prev?.output_file,
          tool_use_id: prev?.tool_use_id,
          task_id: t.task_id,
          task_type: t.task_type || prev?.task_type || "",
          description: t.description || prev?.description || "",
          status: "running",
        })
      }
      for (const t of this.tasks.values()) {
        if (t.status === "running" && !liveIds.has(t.task_id)) {
          t.status = "completed"
          t.ended_at = t.ended_at || Date.now()
        }
      }
      this.emit()
      return
    }
    if (message?.type === "system" && message.subtype === "task_started") {
      if (!message.task_id || message.ambient) return
      const prev = this.tasks.get(message.task_id)
      // Foreground tasks block their tool call and end with it; only the
      // backgrounded ones outlive the turn.
      if (message.is_backgrounded === false && !prev) return
      this.tasks.set(message.task_id, {
        started_at: prev?.started_at ?? Date.now(),
        status: prev?.status ?? "running",
        output_file: prev?.output_file,
        task_id: message.task_id,
        task_type: prev?.task_type || message.task_type || "",
        description: message.description || prev?.description || "",
        tool_use_id: message.tool_use_id || prev?.tool_use_id,
      })
      this.emit()
      return
    }
    if (message?.type === "system" && message.subtype === "task_notification") {
      const t = this.tasks.get(message.task_id)
      if (t) {
        t.status = message.status || "completed"
        t.ended_at = Date.now()
        if (message.output_file) t.output_file = message.output_file
        this.emit()
      }
      return
    }
    // A backgrounded Bash call's tool_result names the output file:
    // "Output is being written to: <path>".
    if (message?.type === "user" && message.tool_use_result?.backgroundTaskId) {
      const id = message.tool_use_result.backgroundTaskId
      for (const block of message.message?.content || []) {
        if (block?.type !== "tool_result") continue
        const parts = typeof block.content === "string" ? [block.content]
          : (block.content || []).map((c: any) => c?.text || "")
        const m = parts.join("\n").match(/Output is being written to: (.+?\.output)/)
        if (!m) continue
        const t: BgTask = this.tasks.get(id) || {
          task_id: id, task_type: "local_bash", description: "",
          status: "running", started_at: Date.now(),
        }
        t.output_file = m[1].trim()
        this.tasks.set(id, t)
        this.emit()
      }
    }
  }
}

// Active agent-loop run state — allows stdin to reach the running query.
//   onFatal:  invoked by the global error handlers to reject the in-flight
//             collection when the SDK loop dies in a background promise.
let activeAgentLoop: {
  query: Query
  inputQueue: AsyncQueue<SDKUserMessage>
  onFatal?: (err: Error) => void
  bgTasks?: BgTaskTracker
  injected?: boolean
} | null = null

// Imported Claude Code sessions kept live between runs, by session id. Their
// claude process idles with the conversation loaded, as in a terminal, until
// a run ends badly or the bridge exits.
const liveImported = new Map<string, {
  query: Query
  inputQueue: AsyncQueue<SDKUserMessage>
  iterator: AsyncIterator<SDKMessage, void>
  bgTasks: BgTaskTracker
  toolNames: string
  totals: { inputTokens: number; outputTokens: number;
            cacheReadTokens: number; cacheCreationTokens: number }
}>()

// -- Injection consumption tracking (§5 delivery ack, finding 8) --
// A delegate_message written to stdin is only "delivered" when the SDK loop
// actually pulls it out of the input queue. We tag each injected message with
// an id and emit @@EVENT@@ user_message_consumed the instant it is dequeued
// into the SDK, so the Python side can distinguish a consumed injection from
// one that raced turn completion and was dropped.
let injectionSeq = 0

// Wrap an AsyncQueue so that each injected SDKUserMessage carrying an
// __atn_injection_id is acked (user_message_consumed) the moment the SDK's
// async iterator reads it. The first (task) message and any message without
// an id pass through untouched.
function attachConsumptionAcks(q: AsyncQueue<SDKUserMessage>): AsyncQueue<SDKUserMessage> {
  const origIterator = q[Symbol.asyncIterator].bind(q)
  ;(q as any)[Symbol.asyncIterator] = (): AsyncIterator<SDKUserMessage> => {
    const inner = origIterator()
    return {
      next: async (): Promise<IteratorResult<SDKUserMessage>> => {
        const r = await inner.next()
        if (!r.done && r.value) {
          const injId = (r.value as any).__atn_injection_id
          if (injId) {
            emitEvent({ type: "user_message_consumed", id: injId })
            log("user_message consumed by SDK loop", { id: injId })
          }
        }
        return r
      },
    }
  }
  return q
}

async function handleAgentLoopRequest(req: AgentLoopRequest): Promise<void> {
  try {
    const model = mapModelToClaudeModel(req.model)
    const maxTurns = req.max_turns || 20

    log("request.agent_loop", {
      model,
      maxTurns,
      toolCount: req.tools.length,
      messageLen: req.message.length,
    })

    // Build in-process MCP server with ATN tools
    const atnServer = buildATNMcpServer(req.tools)

    // Build tool allow-list for auto-approval
    // If native_tools is true, don't restrict — allow all tools including Bash, Read, Write, etc.
    const allowedTools = req.native_tools ? undefined : req.tools.map(t => `mcp__atn__${t.name}`)

    // An imported Claude Code session keeps its query (and so its claude
    // process) open between runs, the way a terminal keeps the session live:
    // the next run's message joins the same query instead of a new resume.
    const imported = req.claude_session && req.claude_session.folder
      ? req.claude_session : null
    const liveKey = imported ? imported.session_id : ""
    // ATN's tools are fixed when a query starts. If this run's surface
    // differs (the agent joined a group, got a new bundle), close the live
    // query and resume the session afresh with the new tools.
    const toolNames = req.tools.map(t => t.name).sort().join(",")
    let reused = liveKey ? liveImported.get(liveKey) : undefined
    if (reused && reused.toolNames !== toolNames) {
      log("request.agent_loop.tools_changed", { session: liveKey })
      liveImported.delete(liveKey)
      try { reused.query.close() } catch {}
      reused = undefined
    }
    if (reused) log("request.agent_loop.reuse", { session: liveKey })

    // Use streaming input (async generator) — required for MCP tools.
    // Queue stays open so mid-turn user messages and interrupt can be injected.
    const inputQueue = reused ? reused.inputQueue : new AsyncQueue<SDKUserMessage>()

    // The message is now just the user's input — no system prompt or history
    // prepended.  System prompt goes via SDK option (cached).  History comes
    // from the SDK session via resume.
    inputQueue.push({
      type: "user",
      message: { role: "user", content: req.message },
      parent_tool_use_id: null,
      session_id: "",
    })

    // NOTE: We do NOT call inputQueue.done() here — the queue stays open
    // so that user_message and interrupt can be injected from stdin.

    // Build SDK options — systemPrompt and resume enable caching and
    // session continuity respectively.
    const sdkOptions: any = {
      maxTurns,
      model,
      pathToClaudeCodeExecutable: claudeExecutable,
      permissionMode: "bypassPermissions",
      allowDangerouslySkipPermissions: true,
      mcpServers: {
        atn: atnServer,
      },
      allowedTools,
      // Block SDK's Agent tool — forces delegation through ATN's create_agent
      // so child work goes through the registry, budget tracking, and event bus.
      // The mcp__claude_ai_* entries strip the account-level claude.ai
      // connectors (Gmail/Calendar/Drive) that ride in on the OAuth login:
      // agents must never see the operator's personal connectors. Server-level
      // rules (no __toolname suffix) cover every tool the connector exposes.
      disallowedTools: [
        "Agent",
        "mcp__claude_ai_Gmail",
        "mcp__claude_ai_Google_Calendar",
        "mcp__claude_ai_Google_Drive",
      ],
      settingSources: [],
      // Only the MCP servers we pass above (the in-process atn server) —
      // ignore every other source, including account-level connectors that
      // the disallow list doesn't know the name of yet.
      strictMcpConfig: true,
    }
    if (pinnedEffort) {
      sdkOptions.effort = pinnedEffort
    }

    if (imported) {
      sdkOptions.cwd = imported.folder
      // User, project and local settings: CLAUDE.md, hooks, MCP servers and
      // permission rules, the same files the user's own session reads.
      sdkOptions.settingSources = ["user", "project", "local"]
      sdkOptions.strictMcpConfig = false
      sdkOptions.systemPrompt = { type: "preset", preset: "claude_code" }
      // Configured like the user's own session: every tool, subagents and
      // claude.ai connectors included, no turn cap, and the effort and
      // context window the user's settings choose rather than ATN's pins.
      sdkOptions.allowedTools = undefined
      sdkOptions.disallowedTools = []
      delete sdkOptions.maxTurns
      delete sdkOptions.effort
      log("request.agent_loop.imported", { cwd: imported.folder })
    }

    // Native built-in tools (Bash/Read/Write/Edit/Glob/Grep/WebSearch/...) are
    // a large chunk of the first-turn context (~tens of thousands of tokens of
    // JSON schemas). `allowedTools` only gates PERMISSIONS — it does not strip
    // the schemas. The `tools` option is the lever: `[]` disables the built-in
    // preset entirely, omitting it loads the full claude_code preset (SDK
    // sdk.d.ts:1190-1199). So unless the agent explicitly asked for native
    // tools, disable them — it still has the ATN MCP tools via mcpServers, and
    // a lean agent (e.g. a simple cognitive responder) carries a much smaller
    // prefix. This shrinks per-agent cold-start cost across the fleet.
    if (!req.native_tools && !imported) {
      sdkOptions.tools = []
    }

    // Enable 1M context window for models that support it (Sonnet 4.x,
    // Opus 4.7+, Fable/Mythos 5).  Version-aware so new releases work unedited.
    if (modelHasLargeContext(model) && !imported) {
      sdkOptions.betas = ["context-1m-2025-08-07"]
      log("request.agent_loop.beta", { betas: sdkOptions.betas })
    }

    // System prompt — passed as SDK option so it gets server-side caching.
    // Falls back to legacy inline system field for backward compat.
    const sysPrompt = req.system_prompt || req.system
    if (sysPrompt && !imported) {
      sdkOptions.systemPrompt = sysPrompt
    }

    // Session continuity — resume a previous session so the SDK loads
    // the conversation history and benefits from prompt caching.
    if (req.session_id) {
      sdkOptions.resume = req.session_id
    }

    log("request.agent_loop.options", {
      hasSystemPrompt: !!sysPrompt,
      resumeSession: req.session_id || null,
      effort: sdkOptions.effort || "sdk-default",
    })
    trace({
      type: "bridge_options",
      session_id: req.session_id || "new",
      model,
      effort: sdkOptions.effort || "sdk-default",
      maxTurns,
    })

    // Ack injected messages the moment the SDK dequeues them (finding 8).
    if (!reused) attachConsumptionAcks(inputQueue)

    const q = reused ? reused.query : query({
      prompt: inputQueue,
      options: sdkOptions,
    })
    // Set when this run ends cleanly on an imported session: the query stays
    // open for the next run.
    let keepAlive = false

    // onFatal lets the global unhandledRejection/uncaughtException handlers
    // abort THIS collection loop (via a rejected race) instead of leaving the
    // for-await hanging forever on an SDK loop that died in the background.
    let fatalReject: ((err: Error) => void) | null = null
    const fatalPromise = new Promise<never>((_, reject) => { fatalReject = reject })

    // Expose to stdin handler for interrupt / user_message
    const bgTasks = reused ? reused.bgTasks : new BgTaskTracker()
    activeAgentLoop = {
      query: q,
      inputQueue,
      onFatal: (err: Error) => { if (fatalReject) fatalReject(err) },
      bgTasks,
    }
    // While the session waits on background tasks the stream can go quiet
    // for minutes; a heartbeat keeps the Python idle ceiling from firing.
    let bgHeartbeat: ReturnType<typeof setInterval> | null = null

    // Collect the full response
    let text = ""
    const thinking: string[] = []
    let resolvedModel: string | null = null
    let inputTokens = 0
    let outputTokens = 0
    let cacheReadTokens = 0
    let cacheCreationTokens = 0
    let wasInterrupted = false
    // Track the last assistant message's usage for accurate context occupancy.
    // modelUsage sums across ALL rounds in the agentic loop, but for "context
    // used" we only care about the most recent round's input tokens.
    let lastRoundInputTokens = 0
    let numTurns = 0
    let totalCostUsd = 0
    let contextWindow = 0
    let maxOutputTokens = 0
    let sessionId = ""
    let fatalError: string | null = null

    // First-event watchdog (§11): the SDK must produce its FIRST stream
    // message within ATN_BRIDGE_FIRST_EVENT_TIMEOUT. A model the SDK loop
    // rejects (haiku on agent loop) can otherwise sit at 100% CPU emitting
    // nothing. We only guard the FIRST .next(); once the stream is producing,
    // long quiet stretches are normal (tools run in-process) and the Python
    // idle ceiling covers mid-run silence.
    const firstEventTimeoutMs =
      Number(process.env.ATN_BRIDGE_FIRST_EVENT_TIMEOUT || "120") * 1000
    let firstMessageSeen = false

    const orchIterator = reused ? reused.iterator : q[Symbol.asyncIterator]()

    try {
      while (true) {
        let iterResult: IteratorResult<SDKMessage, void>
        if (!firstMessageSeen) {
          // Race the first .next() against the watchdog and the fatal signal.
          let watchdog: ReturnType<typeof setTimeout> | null = null
          const watchdogPromise = new Promise<never>((_, reject) => {
            watchdog = setTimeout(() => {
              reject(new Error(
                `no bridge event within ${firstEventTimeoutMs / 1000}s ` +
                `(model=${model}) — SDK loop appears wedged`))
            }, firstEventTimeoutMs)
          })
          try {
            iterResult = await Promise.race([
              orchIterator.next(),
              watchdogPromise,
              fatalPromise,
            ])
          } finally {
            if (watchdog) clearTimeout(watchdog)
          }
          firstMessageSeen = true
        } else {
          // After the first message, still race the fatal signal so a
          // background SDK rejection unblocks a hung .next().
          iterResult = await Promise.race([orchIterator.next(), fatalPromise])
        }
        if (iterResult.done) break
        const message = iterResult.value
        trace(message)
        log("agent_loop.message", {
          type: message.type,
          subtype: (message as any).subtype,
        })
        bgTasks.handle(message)

        if (message.type === "system" && (message as any).subtype === "init") {
          const initModel = (message as any).model
          if (initModel && typeof initModel === "string") {
            resolvedModel = initModel
          }
        }

        // Subscription-quota updates (Claude Max)
        if (message.type === "rate_limit_event") {
          handleRateLimitEvent(message)
        }

        // Detect interrupt result
        if (message.type === "system" && (message as any).subtype === "interrupt") {
          wasInterrupted = true
          log("agent_loop.interrupted")
        }

        // Detect compaction status
        if (message.type === "system" && (message as any).subtype === "status") {
          const status = (message as any).status
          log("agent_loop.status", { status })
          emitEvent({ type: "status", status: status ?? "idle" })
        }

        // Detect compaction boundary
        if (message.type === "system" && (message as any).subtype === "compact_boundary") {
          const meta = (message as any).compact_metadata ?? {}
          log("agent_loop.compact_boundary", meta)
          emitEvent({
            type: "compaction",
            trigger: meta.trigger ?? "auto",
            pre_tokens: meta.pre_tokens ?? 0,
          })
        }

        if (message.type === "assistant") {
          if (!resolvedModel && (message as any).message?.model) {
            resolvedModel = (message as any).message.model
          }
          // Track per-response usage for context occupancy (BetaMessage.usage)
          const msgUsage = (message as any).message?.usage
          if (msgUsage) {
            lastRoundInputTokens =
              (msgUsage.input_tokens ?? 0) +
              (msgUsage.cache_read_input_tokens ?? 0) +
              (msgUsage.cache_creation_input_tokens ?? 0)
            // Inner-loop budget enforcement: emit a per-turn usage event so the
            // Python side can roll into cascading budgets and abort mid-run
            // if a cap is crossed. Includes model so the per-class estimator
            // can attribute tokens correctly.
            const turnModel = (message as any).message?.model || resolvedModel || ""
            emitEvent({
              type: "usage",
              model: turnModel,
              input_tokens: msgUsage.input_tokens ?? 0,
              output_tokens: msgUsage.output_tokens ?? 0,
              cache_read_input_tokens: msgUsage.cache_read_input_tokens ?? 0,
              cache_creation_input_tokens: msgUsage.cache_creation_input_tokens ?? 0,
            })
          }
          for (const block of message.message.content) {
            if (block.type === "text" && block.text) {
              text += block.text
              emitEvent({ type: "text_delta", text: block.text })
            } else if (block.type === "thinking") {
              const thinkText = (block as any).thinking || ""
              if (thinkText) {
                thinking.push(thinkText)
                emitEvent({ type: "thinking", text: thinkText })
              }
            } else if (block.type === "tool_use") {
              // Emit tool_use_start for SDK built-in tool calls (Read, Write, Bash, etc.)
              // These are handled internally by the SDK and don't go through relayToolCall.
              emitEvent({
                type: "tool_use_start",
                tool_use_id: (block as any).id || "",
                tool_name: (block as any).name || "",
                input: (block as any).input || {},
              })
            }
          }
        }

        // Tool use summary — SDK reports a text summary after tool calls complete.
        // The message has: { summary, preceding_tool_use_ids, uuid, session_id }
        if (message.type === "tool_use_summary") {
          const summaryMsg = message as any
          const toolUseIds: string[] = summaryMsg.preceding_tool_use_ids || []
          const summary: string = summaryMsg.summary || ""
          for (const toolUseId of toolUseIds) {
            emitEvent({
              type: "tool_use_result",
              tool_use_id: toolUseId,
              tool_name: "",  // not available in summary message
              is_error: false,
              result_preview: summary.slice(0, 500),
            })
          }
        }

        if (message.type === "result") {
          const resultMsg = message as any
          numTurns = resultMsg.num_turns ?? 0
          totalCostUsd = resultMsg.total_cost_usd ?? 0
          sessionId = resultMsg.session_id ?? ""

          const modelUsage = resultMsg.modelUsage as Record<string, any> | undefined
          if (modelUsage) {
            // modelUsage is the running total for this query(); a session
            // kept open for background tasks yields several results.
            inputTokens = 0
            outputTokens = 0
            cacheReadTokens = 0
            cacheCreationTokens = 0
            for (const mu of Object.values(modelUsage)) {
              if (mu && typeof mu === "object") {
                inputTokens += mu.inputTokens ?? 0
                outputTokens += mu.outputTokens ?? 0
                cacheReadTokens += mu.cacheReadInputTokens ?? 0
                cacheCreationTokens += mu.cacheCreationInputTokens ?? 0
                // Take the largest context window (in case multiple models used)
                if ((mu.contextWindow ?? 0) > contextWindow) {
                  contextWindow = mu.contextWindow ?? 0
                }
                if ((mu.maxOutputTokens ?? 0) > maxOutputTokens) {
                  maxOutputTokens = mu.maxOutputTokens ?? 0
                }
              }
            }
          }

          // Fallback: if SDK didn't populate contextWindow, derive from model name
          if (contextWindow === 0 && resolvedModel) {
            const m = resolvedModel.toLowerCase()
            if (m.includes("claude") || m.includes("fable") || m.includes("mythos")) {
              // 1M-context families (Sonnet 4.x, Opus 4.7+, Fable/Mythos 5)
              // get 1M; older Opus/Haiku get 200k.
              contextWindow = modelHasLargeContext(m) ? 1_000_000 : 200_000
              maxOutputTokens = maxOutputTokens || (m.includes("opus") ? 32_000 : 16_000)
            }
          }

          log("agent_loop.result", {
            textLen: text.length,
            thinkingBlocks: thinking.length,
            interrupted: wasInterrupted,
            numTurns,
            sessionId,
            usage: { inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens },
            contextWindow,
          })

          // Background tasks still running: keep the session open. The SDK
          // wakes the agent when one finishes and a later result follows.
          const live = bgTasks.live()
          if (live.length > 0 && !wasInterrupted) {
            log("agent_loop.waiting_background", { count: live.length })
            emitEvent({ type: "waiting_background", count: live.length })
            if (!bgHeartbeat) {
              bgHeartbeat = setInterval(() => {
                emitEvent({ type: "bg_heartbeat", count: bgTasks.live().length })
              }, 60_000)
            }
            if (text && !text.endsWith("\n")) text += "\n\n"
            continue
          }

          // A message injected during this run may still be queued in the
          // claude process; a live query would answer it with nobody
          // reading. Close instead, and the next run resumes the session.
          if (imported && !wasInterrupted && !activeAgentLoop?.injected) {
            // The session stays live for the next run (see liveImported).
            keepAlive = true
            break
          }

          // Close the input queue — this turn is done.
          // If a user_message was already pushed before this result, the SDK
          // will have consumed it.  Any future user_messages should start a
          // new agent-loop run, not inject into a completed one.
          try { inputQueue.done() } catch {}
          break
        }
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      log("agent_loop.error", { error: errMsg })
      // Surface the failure as a stream event (§11): an SDK error, a
      // first-event-watchdog timeout, or a background fatal reject must never
      // be a silent loop exit. The Python side keys @@EVENT@@ error / done.
      emitEvent({ type: "error", error: errMsg })
      // Kill the query so the SDK's internal loop can't keep spinning after we
      // give up on it (the wedge / haiku-spin case).
      try { q.close() } catch {}
      if (!text) {
        fatalError = errMsg
        text = `[Bridge: agent-loop run error: ${errMsg}]`
      }
    } finally {
      if (bgHeartbeat) clearInterval(bgHeartbeat)
      // A run that ends with tasks still live (interrupt, error) stops them:
      // once the session closes nobody could see or stop them again.
      for (const t of bgTasks.live()) {
        try { await q.stopTask(t.task_id) } catch {}
        t.status = "stopped"
        t.ended_at = Date.now()
      }
      if (bgTasks.tasks.size > 0) {
        emitEvent({ type: "background_tasks", tasks: [...bgTasks.tasks.values()] })
      }
      if (keepAlive) {
        liveImported.set(liveKey, {
          query: q, inputQueue, iterator: orchIterator, bgTasks, toolNames,
          totals: { inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens },
        })
      } else {
        if (liveKey && liveImported.has(liveKey)) {
          liveImported.delete(liveKey)
          try { q.close() } catch {}
        }
        // Clean up — close the input queue and clear active agent-loop run
        try { inputQueue.done() } catch {}
      }
      activeAgentLoop = null
    }

    // modelUsage is the query's running total; a live session reports only
    // what this run added.
    if (reused) {
      inputTokens -= reused.totals.inputTokens
      outputTokens -= reused.totals.outputTokens
      cacheReadTokens -= reused.totals.cacheReadTokens
      cacheCreationTokens -= reused.totals.cacheCreationTokens
    }

    emitEvent({ type: "done" })

    // A fatal error with no assistant output is a hard request failure, not a
    // successful empty turn — report ok:false so the provider raises instead
    // of returning a phantom empty response.
    if (fatalError && !resolvedModel && numTurns === 0) {
      respond({
        id: req.id,
        ok: false,
        error: fatalError,
        session_id: sessionId || undefined,
      })
      return
    }

    respond({
      id: req.id,
      ok: true,
      session_id: sessionId || undefined,
      text,
      thinking: thinking.length > 0 ? thinking : undefined,
      stop_reason: wasInterrupted ? "interrupted" : (fatalError ? "provider_error" : "end_turn"),
      usage: {
        input_tokens: inputTokens,
        output_tokens: outputTokens,
        cache_read_input_tokens: cacheReadTokens,
        cache_creation_input_tokens: cacheCreationTokens,
        last_round_input_tokens: lastRoundInputTokens,
      },
      context: {
        num_turns: numTurns,
        total_cost_usd: totalCostUsd,
        context_window: contextWindow,
        max_output_tokens: maxOutputTokens,
      },
      model: resolvedModel || undefined,
    })
  } catch (e) {
    respond({
      id: req.id,
      ok: false,
      error: e instanceof Error ? e.message : String(e),
    })
  }
}

async function handleRequest(req: BridgeRequest): Promise<void> {
  switch (req.type) {
    case "create": {
      try {
        const model = mapModelToClaudeModel(req.model)

        log("request.create", { model, messageLen: req.message.length })

        const createOptions: Options = {
          maxTurns: 1,
          model,
          pathToClaudeCodeExecutable: claudeExecutable,
          permissionMode: "bypassPermissions",
          allowDangerouslySkipPermissions: true,
          // Single-shot completions need no tools at all — strip the
          // built-in preset and any account-level connectors (see the
          // agent loop options for rationale).
          tools: [],
          settingSources: [],
          strictMcpConfig: true,
        }
        if (pinnedEffort) {
          (createOptions as any).effort = pinnedEffort
        }
        const session = sessionManager.createAndSend(req.message, req.system || "", createOptions)

        const { text, thinking, toolCalls, usage, model: resolvedModel, stopReason } =
          await sessionManager.collectResponse(session)

        sessionManager.delete(session.id)

        respond({
          id: req.id,
          ok: true,
          session_id: session.id,
          text: text || "",
          thinking: thinking.length > 0 ? thinking : undefined,
          tool_calls: toolCalls.length > 0 ? toolCalls : undefined,
          stop_reason: stopReason,
          usage,
          model: resolvedModel || undefined,
        })
      } catch (e) {
        respond({
          id: req.id,
          ok: false,
          error: e instanceof Error ? e.message : String(e),
        })
      }
      break
    }

    case "agent_loop": {
      await handleAgentLoopRequest(req)
      break
    }

    case "delete": {
      const deleted = sessionManager.delete(req.session_id)
      respond({ id: req.id, ok: true })
      break
    }

    case "ping": {
      respond({ id: req.id, ok: true, type: "pong" })
      break
    }

    case "get_usage": {
      try {
        const r = await fetchUsage()
        respond({ ...r, id: req.id })
      } catch (e) {
        const reason = e instanceof Error ? e.message : String(e)
        log("get_usage failed", { error: reason })
        respond({ id: req.id, ok: true, available: false, reason })
      }
      break
    }

    case "session_context": {
      try {
        const sessionId = (req as SessionContextRequest).session_id
        if (!sessionId) {
          respond({ id: req.id, ok: false, error: "Missing session_id" })
          break
        }

        log("request.session_context", { sessionId })
        const rawMessages = await getSessionMessages(sessionId)

        // Extract text content from each message
        const messages = rawMessages.map((m) => {
          let content = ""
          const msg = m.message as any
          if (typeof msg === "string") {
            content = msg
          } else if (msg?.content) {
            if (typeof msg.content === "string") {
              content = msg.content
            } else if (Array.isArray(msg.content)) {
              content = msg.content
                .filter((b: any) => b?.type === "text" && b?.text)
                .map((b: any) => b.text)
                .join("")
            }
          }
          return { role: m.type, content }
        })

        respond({
          id: req.id,
          ok: true,
          messages,
          stats: {
            total_messages: rawMessages.length,
            session_id: sessionId,
          },
        })
      } catch (e) {
        respond({
          id: req.id,
          ok: false,
          error: e instanceof Error ? e.message : String(e),
        })
      }
      break
    }

    case "shutdown": {
      log("shutdown requested")
      sessionManager.destroy()
      respond({ id: req.id, ok: true })
      setTimeout(() => process.exit(0), 100)
      break
    }

    default: {
      respond({
        id: (req as any).id || "unknown",
        ok: false,
        error: `Unknown request type: ${(req as any).type}`,
      })
    }
  }
}

// -- stdin line reader --
// Routes tool_result, interrupt, and user_message to active agent-loop run,
// everything else to handleRequest.

const rl = createInterface({ input: process.stdin })

rl.on("line", async (line: string) => {
  const trimmed = line.trim()
  if (!trimmed) return

  try {
    const parsed = JSON.parse(trimmed)

    // Route tool_result messages to the pending tool call
    if (parsed.type === "tool_result" && parsed.call_id) {
      handleToolResult(parsed.call_id, parsed.result)
      return
    }

    // Interrupt the active agent-loop run
    if (parsed.type === "interrupt") {
      if (activeAgentLoop) {
        log("interrupt requested")
        try {
          await activeAgentLoop.query.interrupt()
        } catch (err) {
          log("interrupt error", {
            error: err instanceof Error ? err.message : String(err),
          })
        }
      } else {
        log("interrupt requested but no active agent-loop run")
      }
      return
    }

    // Stop one background task of the active agent-loop run.
    if (parsed.type === "stop_task" && parsed.task_id) {
      if (activeAgentLoop) {
        log("stop_task requested", { taskId: parsed.task_id })
        try {
          await activeAgentLoop.query.stopTask(parsed.task_id)
        } catch (err) {
          log("stop_task error", {
            error: err instanceof Error ? err.message : String(err),
          })
        }
      } else {
        log("stop_task requested but no active agent-loop run")
      }
      return
    }

    // Push a user message into the active agent-loop run.
    // The Python side assigns an id (parsed.id); we tag the queued message with
    // __atn_injection_id so attachConsumptionAcks emits user_message_consumed
    // the instant the SDK dequeues it. If no agent-loop run is active, the write
    // raced turn completion — reply with a not-consumed ack so the Python side
    // routes the content to the inbox fallback (nothing is silently dropped).
    if (parsed.type === "user_message" && parsed.content) {
      const injId = parsed.id || `inj-${++injectionSeq}`
      if (activeAgentLoop) {
        log("user_message injected", { contentLen: parsed.content.length, id: injId })
        const injMsg: any = {
          type: "user",
          message: { role: "user", content: parsed.content },
          parent_tool_use_id: null,
          session_id: "",
        }
        injMsg.__atn_injection_id = injId
        try {
          activeAgentLoop.inputQueue.push(injMsg)
          activeAgentLoop.injected = true
        } catch (err) {
          // Queue already closed (turn ended between the active-check and the
          // push) — treat as not consumed so Python falls back to the inbox.
          log("user_message push failed (queue closed)", { id: injId })
          emitEvent({ type: "user_message_dropped", id: injId })
        }
      } else {
        log("user_message received but no active agent-loop run", { id: injId })
        emitEvent({ type: "user_message_dropped", id: injId })
      }
      return
    }

    // Everything else is a request
    await handleRequest(parsed as BridgeRequest)
  } catch (e) {
    respond({
      id: "parse-error",
      ok: false,
      error: `Failed to parse request: ${e instanceof Error ? e.message : String(e)}`,
    })
  }
})

rl.on("close", () => {
  log("stdin closed, shutting down")
  sessionManager.destroy()
  process.exit(0)
})

log("bridge ready", { pid: process.pid })
