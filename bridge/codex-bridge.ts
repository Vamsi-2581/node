/**
 * codex-bridge.ts -- stdin/stdout bridge between ATN and OpenAI Codex SDK.
 *
 * Protocol (NDJSON over stdin/stdout — same as claude-bridge.ts):
 *   stdin  (ATN -> bridge): JSON requests + tool_result messages
 *   stdout (bridge -> ATN): JSON responses + tool_call messages
 *   stderr: debug logging + @@EVENT@@ streaming events
 *
 * Request types:
 *   create      — single-turn LLM call (one prompt → one response)
 *   agent loop — multi-turn with Codex's built-in tools + ATN tool relay via MCP
 *   shutdown    — graceful exit
 *
 * ATN Tool Relay Architecture:
 *   When tools are provided in an agent loop request, the bridge:
 *   1. Starts a local TCP relay server on an ephemeral port
 *   2. Passes MCP server config to Codex SDK (--config mcp_servers.atn=...)
 *   3. Codex CLI spawns codex-mcp-relay.ts as its MCP server
 *   4. The relay connects back to our TCP server
 *   5. Tool calls flow: Codex CLI → MCP relay → TCP → bridge → stdout → Python
 *   6. Results flow back the reverse path
 *
 * This gives Codex native structured MCP tool calling — same as Claude's
 * in-process MCP server, but via an external relay process.
 */

import { Codex, type Thread, type ThreadEvent, type ThreadItem, type Usage } from "@openai/codex-sdk"
import { randomUUID } from "crypto"
import { createInterface } from "readline"
import { createServer, type Server as NetServer, type Socket } from "net"
import { fileURLToPath } from "url"
import { join, dirname } from "path"
import { spawn } from "child_process"
import { createRequire } from "module"
import { existsSync } from "fs"

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// -- Logging (stderr only) --

function log(msg: string, extra?: Record<string, unknown>): void {
  const parts = ["[codex-bridge]", msg]
  if (extra && Object.keys(extra).length > 0) {
    parts.push(JSON.stringify(extra))
  }
  process.stderr.write(parts.join(" ") + "\n")
}

// -- Streaming events (@@EVENT@@ on stderr) --

const EVENT_PREFIX = "@@EVENT@@"

function emitEvent(event: { type: string; [key: string]: unknown }): void {
  process.stderr.write(`${EVENT_PREFIX}${JSON.stringify(event)}\n`)
}

// -- Global error handlers --

process.on("unhandledRejection", (reason: unknown) => {
  log("unhandled rejection (kept alive)", {
    error: reason instanceof Error ? reason.message : String(reason),
  })
})

process.on("uncaughtException", (err: Error) => {
  log("uncaught exception (kept alive)", {
    error: err.message,
  })
})

// -- ATN tool relay --
// During agent loop, ATN tools are relayed to Python for execution.
// Tool calls arrive from the MCP relay via TCP, get forwarded to Python
// via stdout, and results flow back.

const pendingToolCalls = new Map<string, {
  resolve: (result: any) => void
  reject: (error: Error) => void
}>()

function relayToolCall(name: string, input: unknown): Promise<any> {
  const callId = randomUUID().slice(0, 12)
  // No emitEvent here: the SDK's item.started/item.completed thread events
  // already cover this call (with richer data — result_preview on complete).
  // Emitting from the relay too made every tool call appear TWICE in the UI.
  return new Promise((resolve, reject) => {
    pendingToolCalls.set(callId, { resolve, reject })
    const msg = JSON.stringify({
      type: "tool_call",
      call_id: callId,
      name,
      input,
    })
    process.stdout.write(msg + "\n")
    log("tool_call relayed to Python", { callId, name })
  })
}

function handleToolResult(callId: string, result: any): void {
  const pending = pendingToolCalls.get(callId)
  if (pending) {
    pendingToolCalls.delete(callId)
    pending.resolve(result)
    // No emitEvent — see relayToolCall: the SDK item.completed event carries
    // the result for the UI.
    log("tool_result received from Python", { callId })
  } else {
    // Check if it's for the TCP relay
    const relayCb = pendingRelayResults.get(callId)
    if (relayCb) {
      pendingRelayResults.delete(callId)
      relayCb(result)
    } else {
      log("tool_result for unknown call_id", { callId })
    }
  }
}

// -- TCP Relay Server --
// Listens for connections from codex-mcp-relay.ts.  When the relay
// receives an MCP tool_call from the Codex CLI, it forwards it here.
// We relay to Python and send the result back.

let relayServer: NetServer | null = null
let relayPort = 0
let relaySocket: Socket | null = null
const pendingRelayResults = new Map<string, (result: any) => void>()

interface ATNToolDef {
  name: string
  description: string
  input_schema: Record<string, any>
}

async function startRelayServer(): Promise<number> {
  return new Promise((resolve, reject) => {
    relayServer = createServer((socket) => {
      log("MCP relay connected")
      relaySocket = socket

      const socketRl = createInterface({ input: socket })

      socketRl.on("line", async (line: string) => {
        try {
          const msg = JSON.parse(line)
          if (msg.type === "tool_call" && msg.call_id && msg.name) {
            log("tool_call from MCP relay", { callId: msg.call_id, name: msg.name })

            // Relay to Python and wait for result
            try {
              const result = await relayToolCall(msg.name, msg.input || {})

              // Send result back to the MCP relay
              const resp = JSON.stringify({
                type: "tool_result",
                call_id: msg.call_id,
                result,
              }) + "\n"
              socket.write(resp)
            } catch (err) {
              const resp = JSON.stringify({
                type: "tool_result",
                call_id: msg.call_id,
                result: { error: err instanceof Error ? err.message : String(err) },
              }) + "\n"
              socket.write(resp)
            }
          }
        } catch {
          log("bad message from MCP relay", { line: line.slice(0, 200) })
        }
      })

      socket.on("close", () => {
        log("MCP relay disconnected")
        relaySocket = null
      })

      socket.on("error", (err) => {
        log("MCP relay socket error", { error: err.message })
      })
    })

    relayServer.listen(0, "127.0.0.1", () => {
      const addr = relayServer!.address()
      if (addr && typeof addr === "object") {
        relayPort = addr.port
        log("relay server listening", { port: relayPort })
        resolve(relayPort)
      } else {
        reject(new Error("Failed to get relay server address"))
      }
    })

    relayServer.on("error", reject)
  })
}

function stopRelayServer(): void {
  if (relaySocket) {
    relaySocket.destroy()
    relaySocket = null
  }
  if (relayServer) {
    relayServer.close()
    relayServer = null
  }
  relayPort = 0
}

// -- Model mapping --

// ChatGPT-account auth accepts only current Codex models (verified
// empirically 2026-08: gpt-5.6-terra, gpt-5.5). Retired ids (o1, o3,
// o4-mini, gpt-4*) are rejected by the service with "not supported when
// using Codex with a ChatGPT account". There is no default model (decision
// 2026-10-06): a missing, retired or non-gpt id is a loud error naming the
// model, never a silent remap onto some other model.
const RETIRED_MODELS = /^(o[134]|gpt-4)/

function mapModel(model: string | undefined): string {
  if (!model) {
    throw new Error(
      "No model specified: the agent must name its Codex model explicitly (no default model)",
    )
  }
  if (RETIRED_MODELS.test(model)) {
    throw new Error(
      `Model '${model}' is retired: not supported with a ChatGPT account (use a current Codex model such as gpt-5.6-terra or gpt-5.5)`,
    )
  }
  if (!model.startsWith("gpt")) {
    throw new Error(
      `Model '${model}' is not a Codex model: the codex_max bridge serves gpt-* models only`,
    )
  }
  return model
}

// -- Session tracking --

interface CodexSession {
  id: string
  thread: Thread
  threadId: string | null
  createdAt: number
}

const sessions = new Map<string, CodexSession>()

// -- Codex instance --
// Created per-run when tools are present (to pass MCP config),
// or lazily for simple create requests.

function createCodex(mcpConfig?: Record<string, any>): Codex {
  const apiKey = process.env.CODEX_API_KEY || process.env.OPENAI_API_KEY || ""
  const options: any = {
    apiKey: apiKey || undefined,
  }
  if (mcpConfig) {
    options.config = { mcp_servers: mcpConfig }
  }
  return new Codex(options)
}

let simpleCodex: Codex | null = null

function getSimpleCodex(): Codex {
  if (!simpleCodex) {
    simpleCodex = createCodex()
  }
  return simpleCodex
}

// -- Resolve path to the MCP relay script --

function getRelayScriptPath(): string {
  return join(__dirname, "codex-mcp-relay.ts")
}

// -- Build MCP server config for Codex --

function buildMcpConfig(tools: ATNToolDef[], port: number): Record<string, any> {
  // Resolve the runtime — same logic as codex_bridge.py _ensure_process
  const runtime = process.argv[0]  // bun, node, or tsx
  const relayScript = getRelayScriptPath()

  // Determine the right command based on what's running us
  let command: string
  let args: string[]

  if (runtime.includes("bun")) {
    command = "bun"
    args = ["run", relayScript]
  } else if (runtime.includes("tsx")) {
    command = "tsx"
    args = [relayScript]
  } else {
    // node with tsx loader
    command = "node"
    args = ["--import", "tsx", relayScript]
  }

  return {
    atn: {
      command,
      args,
      env: {
        ATN_RELAY_PORT: String(port),
        ATN_TOOLS_JSON: JSON.stringify(tools),
      },
      // Codex CLI >= 0.149 gates MCP tool calls behind per-tool approval;
      // with approvalPolicy "never" an unapproved call is auto-DENIED
      // ("MCP tool call requires approval, but approval policy is never").
      // "approve" = unconditionally approved (codex-rs mcp_tool_call.rs:
      // Auto consults annotations we don't set, Approve never asks).
      // ATN tools are our own relay — approve them unconditionally.
      default_tools_approval_mode: "approve",
    },
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
  system_prompt?: string
  model?: string
  tools: ATNToolDef[]
  max_turns?: number
  session_id?: string
  working_directory?: string
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

type BridgeRequest = CreateRequest | AgentLoopRequest | DeleteRequest | ShutdownRequest | PingRequest | GetUsageRequest

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
  items?: ThreadItem[]
  // get_usage response
  available?: boolean
  reason?: string
  plan_type?: string | null
  rate_limits?: Record<string, Record<string, unknown>>
}

// -- Response writer --

function respond(resp: BridgeResponse): void {
  process.stdout.write(JSON.stringify(resp) + "\n")
}

// -- Subscription usage (get_usage) --
// The sanctioned source is the Codex CLI's own app-server: spawn
// `codex app-server` (stdio JSON-RPC), handshake, then read
// account/rateLimits/read. The CLI reads its own login; this bridge never
// touches ~/.codex/auth.json. Short-lived per request; callers cache.

const USAGE_TIMEOUT_MS = 30_000

function resolveCodexBinary(): string | null {
  if (process.env.CODEX_PATH_OVERRIDE && existsSync(process.env.CODEX_PATH_OVERRIDE)) {
    return process.env.CODEX_PATH_OVERRIDE
  }
  const triples: Record<string, [string, string]> = {
    "win32-x64": ["x86_64-pc-windows-msvc", "@openai/codex-win32-x64"],
    "win32-arm64": ["aarch64-pc-windows-msvc", "@openai/codex-win32-arm64"],
    "linux-x64": ["x86_64-unknown-linux-musl", "@openai/codex-linux-x64"],
    "linux-arm64": ["aarch64-unknown-linux-musl", "@openai/codex-linux-arm64"],
    "darwin-x64": ["x86_64-apple-darwin", "@openai/codex-darwin-x64"],
    "darwin-arm64": ["aarch64-apple-darwin", "@openai/codex-darwin-arm64"],
  }
  const t = triples[`${process.platform}-${process.arch}`]
  if (!t) return null
  const [triple, pkg] = t
  const bin = process.platform === "win32" ? "codex.exe" : "codex"
  try {
    const sdkReq = createRequire(import.meta.url)
    const codexPkg = sdkReq.resolve("@openai/codex/package.json")
    const platPkg = createRequire(codexPkg).resolve(`${pkg}/package.json`)
    const root = join(dirname(platPkg), "vendor", triple)
    for (const cand of [join(root, "bin", bin), join(root, "codex", bin)]) {
      if (existsSync(cand)) return cand
    }
  } catch {}
  return null
}

function windowKey(mins: number | null | undefined): string {
  // Bucket by duration so the provider card's named slots line up with
  // the claude bridge (paid plans: 5h + weekly; free: a longer window).
  if (!mins) return "primary"
  if (mins <= 6 * 60) return "five_hour"
  if (mins <= 8 * 24 * 60) return "seven_day"
  return `${Math.round(mins / 1440)}d`
}

// Map a RateLimitSnapshot onto the rate_limit entry shape the claude
// bridge uses (utilization 0-1, resetsAt epoch seconds).
function mapCodexSnapshot(snap: any): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {}
  if (!snap || typeof snap !== "object") return out
  const now = Date.now()
  const reached = snap.rateLimitReachedType != null
  for (const w of [snap.primary, snap.secondary]) {
    if (!w || typeof w.usedPercent !== "number") continue
    const key = windowKey(w.windowDurationMins)
    out[key] = {
      status: reached || w.usedPercent >= 100 ? "limit_reached" : "allowed",
      rateLimitType: key,
      utilization: w.usedPercent / 100,
      resetsAt: w.resetsAt ?? undefined,
      windowSeconds: w.windowDurationMins ? w.windowDurationMins * 60 : undefined,
      updatedAt: now,
    }
  }
  return out
}

function readCodexRateLimits(): Promise<any> {
  const bin = resolveCodexBinary()
  if (!bin) return Promise.reject(new Error("codex binary not found"))
  return new Promise((resolve, reject) => {
    const child = spawn(bin, ["app-server"], { stdio: ["pipe", "pipe", "ignore"], windowsHide: true })
    let done = false
    const finish = (err: Error | null, val?: any) => {
      if (done) return
      done = true
      clearTimeout(timer)
      try { child.stdin.end() } catch {}
      try { child.kill() } catch {}
      err ? reject(err) : resolve(val)
    }
    const timer = setTimeout(() => finish(new Error("app-server timed out")), USAGE_TIMEOUT_MS)
    const send = (msg: Record<string, unknown>) => child.stdin.write(JSON.stringify(msg) + "\n")
    child.on("error", (e) => finish(e))
    child.on("exit", (code) => finish(new Error(`app-server exited (${code})`)))
    createInterface({ input: child.stdout }).on("line", (line) => {
      let msg: any
      try { msg = JSON.parse(line) } catch { return }
      if (msg.id === 1) {
        if (msg.error) return finish(new Error(`initialize: ${msg.error.message || "error"}`))
        send({ method: "initialized" })
        send({ id: 2, method: "account/rateLimits/read" })
      } else if (msg.id === 2) {
        if (msg.error) return finish(new Error(`rateLimits/read: ${msg.error.message || "error"}`))
        finish(null, msg.result)
      }
    })
    send({ id: 1, method: "initialize",
           params: { clientInfo: { name: "atn-codex-bridge", title: null, version: "1.0.0" } } })
  })
}

async function handleGetUsage(req: GetUsageRequest): Promise<void> {
  try {
    const result = await readCodexRateLimits()
    const byId = result?.rateLimitsByLimitId || {}
    const snap = byId.codex || result?.rateLimits
    const windows = mapCodexSnapshot(snap)
    if (Object.keys(windows).length === 0) {
      respond({ id: req.id, ok: true, available: false, reason: "no rate-limit windows reported",
                plan_type: snap?.planType ?? null })
      return
    }
    respond({ id: req.id, ok: true, available: true, plan_type: snap?.planType ?? null,
              rate_limits: windows })
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e)
    log("get_usage failed", { error: reason })
    respond({ id: req.id, ok: true, available: false, reason })
  }
}

// -- Handle create request (single-turn) --

async function handleCreate(req: CreateRequest): Promise<void> {
  try {
    const model = mapModel(req.model)
    log("request.create", { model, messageLen: req.message.length })

    const codex = getSimpleCodex()
    const thread = codex.startThread({
      model,
      approvalPolicy: "never",
      sandboxMode: "read-only",
      skipGitRepoCheck: true,
    })

    let prompt = req.message
    if (req.system) {
      prompt = `${req.system}\n\n---\n\n${prompt}`
    }

    const turn = await thread.run(prompt)

    let text = turn.finalResponse || ""
    const thinking: string[] = []

    for (const item of turn.items) {
      if (item.type === "reasoning" && item.text) {
        thinking.push(item.text)
      }
    }

    const usage = turn.usage || { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0 }
    // OpenAI/Codex: cached_input_tokens ⊆ input_tokens.  Normalize to the
    // Anthropic convention used by the rest of ATN (input_tokens = fresh
    // portion only, cache reads tracked separately) so roll-ups don't
    // double-count the cached fraction.
    const cachedTokens = usage.cached_input_tokens || 0
    const freshInputTokens = Math.max(0, usage.input_tokens - cachedTokens)

    respond({
      id: req.id,
      ok: true,
      session_id: thread.id || undefined,
      text,
      thinking: thinking.length > 0 ? thinking : undefined,
      stop_reason: "end_turn",
      usage: {
        input_tokens: freshInputTokens,
        output_tokens: usage.output_tokens,
        cache_read_input_tokens: cachedTokens,
        cache_creation_input_tokens: 0,
      },
      model,
    })
  } catch (e) {
    respond({
      id: req.id,
      ok: false,
      error: e instanceof Error ? e.message : String(e),
    })
  }
}

// -- Handle agent loop request (multi-turn with native MCP tool relay) --

async function handleAgentLoop(req: AgentLoopRequest): Promise<void> {
  try {
    const model = mapModel(req.model)
    const maxTurns = req.max_turns || 20

    log("request.agent_loop", {
      model,
      maxTurns,
      toolCount: req.tools.length,
      messageLen: req.message.length,
    })

    // Start the relay server if tools are provided
    let codex: Codex
    if (req.tools.length > 0) {
      if (!relayPort) {
        await startRelayServer()
      }
      const mcpConfig = buildMcpConfig(req.tools, relayPort)
      codex = createCodex(mcpConfig)
      log("agent_loop.mcp_config", { port: relayPort, tools: req.tools.length })
    } else {
      codex = getSimpleCodex()
    }

    // Resume existing thread or start new
    let thread: Thread
    if (req.session_id) {
      thread = codex.resumeThread(req.session_id, {
        model,
        approvalPolicy: "never",
        sandboxMode: "workspace-write",
        workingDirectory: req.working_directory,
      })
    } else {
      thread = codex.startThread({
        model,
        approvalPolicy: "never",
        sandboxMode: "workspace-write",
        workingDirectory: req.working_directory,
        skipGitRepoCheck: true,
      })
    }

    // Build prompt — system prompt is prepended, ATN tools are native MCP now
    const sysPrompt = req.system_prompt || req.system || ""
    let prompt = req.message
    if (sysPrompt) {
      prompt = `${sysPrompt}\n\n---\n\nUser request: ${prompt}`
    }

    // If tools are present, add a brief note that they're available via MCP
    if (req.tools.length > 0) {
      const toolNames = req.tools.map(t => t.name).join(", ")
      prompt += `\n\n[ATN tools available via MCP server "atn": ${toolNames}]`
    }

    // Stream events
    let text = ""
    const thinking: string[] = []
    const allItems: ThreadItem[] = []
    let totalUsage: Usage = {
      input_tokens: 0, output_tokens: 0, cached_input_tokens: 0,
      cache_write_input_tokens: 0, reasoning_output_tokens: 0,
    }
    let lastRoundInputTokens = 0  // most recent turn's total input (for context occupancy)
    let turnCount = 0
    let sessionId = ""

    const { events } = await thread.runStreamed(prompt)

    for await (const event of events) {
      log("agent_loop.event", { type: event.type })

      switch (event.type) {
        case "thread.started":
          sessionId = event.thread_id
          log("agent_loop.thread_started", { threadId: sessionId })
          break

        case "turn.started":
          turnCount++
          emitEvent({ type: "turn_started", turn: turnCount })
          break

        case "turn.completed":
          if (event.usage) {
            totalUsage.input_tokens += event.usage.input_tokens
            totalUsage.output_tokens += event.usage.output_tokens
            totalUsage.cached_input_tokens += event.usage.cached_input_tokens
            // Codex replays full thread context on every turn, so the
            // last turn's input_tokens is current context occupancy.
            lastRoundInputTokens = event.usage.input_tokens
          }
          break

        case "turn.failed":
          log("agent_loop.turn_failed", { error: event.error.message })
          break

        case "item.started":
        case "item.updated":
          if (event.item.type === "agent_message") {
            const prevLen = text.length
            const delta = event.item.text.slice(prevLen)
            if (delta) {
              emitEvent({ type: "text_delta", text: delta })
              text = event.item.text
            }
          } else if (event.item.type === "command_execution") {
            emitEvent({
              type: "tool_use_start",
              tool_use_id: event.item.id,
              tool_name: "command_execution",
              input: { command: event.item.command },
            })
          } else if (event.item.type === "file_change") {
            emitEvent({
              type: "tool_use_start",
              tool_use_id: event.item.id,
              tool_name: "file_change",
              input: { changes: event.item.changes },
            })
          } else if (event.item.type === "reasoning") {
            emitEvent({ type: "thinking", text: event.item.text })
          } else if (event.item.type === "mcp_tool_call") {
            emitEvent({
              type: "tool_use_start",
              tool_use_id: event.item.id,
              tool_name: event.item.tool,
              input: event.item.arguments,
            })
          }
          break

        case "item.completed":
          allItems.push(event.item)
          if (event.item.type === "agent_message") {
            text = event.item.text
          } else if (event.item.type === "reasoning" && event.item.text) {
            thinking.push(event.item.text)
          } else if (event.item.type === "command_execution") {
            emitEvent({
              type: "tool_use_result",
              tool_use_id: event.item.id,
              tool_name: "command_execution",
              is_error: event.item.status === "failed",
              result_preview: (event.item.aggregated_output || "").slice(0, 500),
            })
          } else if (event.item.type === "file_change") {
            emitEvent({
              type: "tool_use_result",
              tool_use_id: event.item.id,
              tool_name: "file_change",
              is_error: event.item.status === "failed",
            })
          } else if (event.item.type === "mcp_tool_call") {
            // MCP tool calls to our ATN relay show up here when complete
            emitEvent({
              type: "tool_use_result",
              tool_use_id: event.item.id,
              tool_name: event.item.tool,
              is_error: event.item.status === "failed",
              result_preview: event.item.error?.message
                || (event.item.result?.content?.[0] as any)?.text?.slice(0, 500)
                || "",
            })
          }
          break
      }
    }

    emitEvent({ type: "done" })

    // Store session for resume
    if (sessionId) {
      sessions.set(sessionId, {
        id: sessionId,
        thread,
        threadId: sessionId,
        createdAt: Date.now(),
      })
    }

    // Normalize OpenAI shape → ATN shape: cached_input_tokens is a subset
    // of input_tokens.  Report fresh-only as input_tokens; cached separately.
    const totalCachedTokens = totalUsage.cached_input_tokens
    const totalFreshInputTokens = Math.max(0, totalUsage.input_tokens - totalCachedTokens)

    respond({
      id: req.id,
      ok: true,
      session_id: sessionId || undefined,
      text,
      thinking: thinking.length > 0 ? thinking : undefined,
      stop_reason: "end_turn",
      usage: {
        input_tokens: totalFreshInputTokens,
        output_tokens: totalUsage.output_tokens,
        cache_read_input_tokens: totalCachedTokens,
        cache_creation_input_tokens: 0,
        last_round_input_tokens: lastRoundInputTokens,
      },
      context: {
        num_turns: turnCount,
        total_cost_usd: 0,
        context_window: 200_000,
        max_output_tokens: 100_000,
      },
      model,
      items: allItems,
    })
  } catch (e) {
    respond({
      id: req.id,
      ok: false,
      error: e instanceof Error ? e.message : String(e),
    })
  }
}

// -- Request router --

async function handleRequest(req: BridgeRequest): Promise<void> {
  switch (req.type) {
    case "create":
      await handleCreate(req)
      break

    case "agent_loop":
      await handleAgentLoop(req as AgentLoopRequest)
      break

    case "delete": {
      const session = sessions.get(req.session_id)
      if (session) {
        sessions.delete(req.session_id)
        log("session.deleted", { sessionId: req.session_id })
      }
      respond({ id: req.id, ok: true })
      break
    }

    case "ping":
      respond({ id: req.id, ok: true })
      break

    case "get_usage":
      await handleGetUsage(req as GetUsageRequest)
      break

    case "shutdown": {
      log("shutdown requested")
      sessions.clear()
      stopRelayServer()
      respond({ id: req.id, ok: true })
      setTimeout(() => process.exit(0), 100)
      break
    }

    default:
      respond({
        id: (req as any).id || "unknown",
        ok: false,
        error: `Unknown request type: ${(req as any).type}`,
      })
  }
}

// -- stdin line reader --

const rl = createInterface({ input: process.stdin })

rl.on("line", async (line: string) => {
  const trimmed = line.trim()
  if (!trimmed) return

  try {
    const parsed = JSON.parse(trimmed)

    // Route tool_result messages to pending tool calls
    if (parsed.type === "tool_result" && parsed.call_id) {
      handleToolResult(parsed.call_id, parsed.result)
      return
    }

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
  sessions.clear()
  stopRelayServer()
  process.exit(0)
})

log("bridge ready", { pid: process.pid })
