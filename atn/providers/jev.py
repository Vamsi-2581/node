"""Jev provider: TypeSafe's "System One" judgment model as an ATN provider.

What Jev is (docs.typesafe.ai, C:/code/jev): a calibrated judgment model, NOT
an LLM. One request is a ``state`` (string, JSON object or array) plus a map of
typed ``questions``; the reply is one typed answer per question:

  choice  {"type":"choice","instructions":..,"criteria":{key: desc, ..}}
          -> {"choice": key, "probabilities": {key: p}, "confidence": p}
  score   {"type":"score","instructions":..,"criteria":[level0, level1, ..]}
          -> {"score": expected level, "probabilities": [..], "confidence": p}
  noul    {"type":"noul","instructions":"yes/no statement", "criteria"?: ..}
          -> {"noul": p(true)}

Single shot, no memory, no generated text, no tool calls. Endpoint
``POST https://api.typesafe.ai/v1/systemone`` with a Bearer key; billed per
INPUT token ($0.042/Mtok for jev-1.13.0, output free), ~0.3-0.7s per call.

Design choice (honest to that capability):

* ``supports_agent_loop`` is False. Jev cannot fill tool arguments, so it
  never calls tools and never runs the multi-turn tool loop.
* ``run_agent_loop`` is overridden with ONE judgment turn so a Jev agent can
  still be run by the execution engine (which always drives cognitive agents
  through run_agent_loop). The engine's offered tool surface is ignored.
* The daemon refuses to bind tool-using agents to Jev
  (``provider_identity.tool_binding_error``): a Jev agent must have
  ``tools: []``.
* ``send()`` with tools raises (the step path's "single call with tools").

Where the questions come from (first match wins):

1. The last user message is a JSON object with a ``questions`` map and an
   optional ``state`` (when absent, the state is the previous user turn's
   text, else an empty string). This is the per-call protocol: a parent agent
   posts a full Jev request.
2. The system prompt declares the questions in a fenced ``jev`` block::

       ```jev
       {"route": {"type": "choice", "instructions": "Which team?",
                  "criteria": {"billing": "..", "tech": ".."}}}
       ```

   (or ``{"questions": {...}}``). Each incoming message becomes the state:
   ``{"message": <text>}``, plus ``"instructions"`` with the rest of the
   system prompt when there is any. This is the standing protocol for a
   classifier/router agent.

Otherwise the call fails loud: Jev has nothing to answer.

The reply text is the answers map as JSON (``{"route": {"choice": ..}}``),
so code and parent agents read it directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from .base import (
    Provider,
    ProviderError,
    ProviderResponse,
    ToolDefinition,
    Usage,
    _call_recorder,
    _maybe_await,
)

log = logging.getLogger(__name__)

JEV_BASE_URL = "https://api.typesafe.ai/v1"
_QUESTION_TYPES = ("choice", "score", "noul")
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_INITIAL_BACKOFF = 1.0
_TIMEOUT_S = 30.0

_JEV_BLOCK = re.compile(r"```jev\s*\n(.*?)```", re.DOTALL)

NO_QUESTIONS_ERROR = (
    "Jev cannot generate text: it only answers typed questions (choice, "
    "score, noul). Declare the questions in the agent's system prompt in a "
    "```jev fenced JSON block, or send a JSON request "
    '{"state": ..., "questions": {...}} as the message.'
)


class JevProvider(Provider):
    """TypeSafe Jev (System One) judgment provider. See module docstring."""

    # The closing attest_tools review only follows tool use; Jev uses none.
    handles_review_step: bool = True

    def __init__(
        self,
        api_key: str,
        *,
        default_model: str = "",
        base_url: str = "",
        name: str = "jev",
        provider_id: str = "jev",
    ) -> None:
        if not api_key:
            raise ProviderError("No Jev (TypeSafe) API key configured", provider="jev")
        self._name = name
        self._provider_id = provider_id or name
        self._api_key = api_key
        self._default_model = default_model
        self._config_base_url = base_url
        self._base_url = (base_url or JEV_BASE_URL).rstrip("/")

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def supports_agent_loop(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # One judgment call
    # ------------------------------------------------------------------

    async def send(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str = "",
        model: str = "",
        max_tokens: int = 1024,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        model = model or self._default_model
        if not model:
            raise ProviderError(
                "No model specified: the agent must name its model explicitly "
                "(no default model)", provider=self._name)
        if tools:
            raise ProviderError(
                "Jev cannot call tools: it answers typed questions only. Bind "
                "tool-using work to an LLM provider.", provider=self._name)
        state, questions = build_request(messages, system)
        data = await self.ask(state, questions, model=model)
        answers = data.get("answers") or {}
        u = data.get("usage") or {}
        return ProviderResponse(
            text=json.dumps(answers, ensure_ascii=False),
            stop_reason="end_turn",
            usage=Usage(
                input_tokens=int(u.get("input_tokens", 0) or 0),
                output_tokens=int(u.get("output_tokens", 0) or 0),
            ),
            model=data.get("model") or model,
            raw=data,
        )

    async def ask(self, state: Any, questions: dict[str, Any], *,
                  model: str) -> dict[str, Any]:
        """POST one Jev request; returns the decoded response body."""
        body = {"model": model, "state": state, "questions": questions}
        headers = {"authorization": f"Bearer {self._api_key}",
                   "content-type": "application/json"}
        url = f"{self._base_url}/systemone"
        backoff = _INITIAL_BACKOFF
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, headers=headers, json=body)
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    raise ProviderError(f"jev request failed: {exc}",
                                        provider=self._name) from None
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    ra = resp.headers.get("retry-after")
                    try:
                        wait = float(ra) if ra else backoff
                    except ValueError:
                        wait = backoff
                    log.warning("jev %d (attempt %d/%d), retrying in %.1fs",
                                resp.status_code, attempt + 1, _MAX_RETRIES + 1, wait)
                    await asyncio.sleep(wait)
                    backoff *= 2
                    continue
                raise ProviderError(
                    f"jev API error {resp.status_code}: {_error_text(resp)}",
                    status_code=resp.status_code, provider=self._name)
        raise ProviderError("jev: exhausted retries", provider=self._name)

    # ------------------------------------------------------------------
    # Agent-loop entry point: one judgment, no tools
    # ------------------------------------------------------------------

    async def run_agent_loop(
        self,
        *,
        message: str,
        system: str = "",
        model: str = "",
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Any = None,
        usage_recorder: Any = None,
        history: list[dict[str, Any]] | None = None,
        **_unused: Any,
    ) -> ProviderResponse:
        """One Jev judgment over ``message``. The engine always offers a tool
        surface; Jev cannot use it, so it is ignored (never executed)."""
        self._interrupted = False
        model = model or self._default_model
        if model:
            self._active_model = model
        messages = list(history or []) + [{"role": "user", "content": message}]
        response = await self.send(messages=messages, system=system, model=model)

        u = response.usage
        self._cumulative_turns += 1
        self._cumulative_input_tokens += u.input_tokens
        self._cumulative_output_tokens += u.output_tokens
        self._last_input_tokens = u.input_tokens
        if response.model:
            self._active_model = response.model
        try:
            from ..metering import cost_usd
            usd = cost_usd(response.model or model,
                           input_tokens=u.input_tokens,
                           output_tokens=u.output_tokens)
            if usd:
                self._total_cost_usd += usd
        except Exception:
            log.debug("jev cost accumulation failed", exc_info=True)

        if usage_recorder is not None:
            try:
                turn = u.budget_tokens()
                if turn > 0:
                    ok, blocker = await _maybe_await(
                        _call_recorder(usage_recorder, turn, response.model or model))
                    if not ok:
                        log.warning("jev run booked past budget (blocker=%s)", blocker)
            except Exception:
                log.exception("usage_recorder failed; continuing")

        if on_chunk and response.text:
            await on_chunk(response.text)
        return response


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _as_questions(obj: Any) -> dict[str, Any] | None:
    """``obj`` as a validated questions map, or None when it is not one."""
    if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
        obj = obj["questions"]
    if not isinstance(obj, dict) or not obj:
        return None
    for name, q in obj.items():
        if not isinstance(q, dict) or q.get("type") not in _QUESTION_TYPES:
            raise ProviderError(
                f"jev question {name!r} must be an object with type one of "
                f"{', '.join(_QUESTION_TYPES)}", provider="jev")
        if "instructions" not in q:
            raise ProviderError(f"jev question {name!r} needs 'instructions'",
                                provider="jev")
    return obj


def build_request(messages: list[dict[str, Any]], system: str) -> tuple[Any, dict[str, Any]]:
    """(state, questions) for one Jev call. See the module docstring."""
    users = [_text_of(m.get("content")) for m in messages if m.get("role") == "user"]
    last = users[-1].strip() if users else ""

    # 1. The message itself is a Jev request.
    if last.startswith("{"):
        try:
            obj = json.loads(last)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "questions" in obj:
            questions = _as_questions(obj)
            if questions is None:
                raise ProviderError("jev request has an empty 'questions' map",
                                    provider="jev")
            state = obj.get("state")
            if state is None:
                state = users[-2] if len(users) > 1 else ""
            return state, questions

    # 2. Standing questions declared in the system prompt.
    m = _JEV_BLOCK.search(system or "")
    if m:
        try:
            declared = json.loads(m.group(1))
        except ValueError as exc:
            raise ProviderError(f"jev block in the system prompt is not valid JSON: {exc}",
                                provider="jev") from None
        questions = _as_questions(declared)
        if questions is None:
            raise ProviderError("jev block in the system prompt has no questions",
                                provider="jev")
        state: dict[str, Any] = {"message": last}
        rest = _JEV_BLOCK.sub("", system).strip()
        if rest:
            state["instructions"] = rest
        return state, questions

    raise ProviderError(NO_QUESTIONS_ERROR, provider="jev")


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, dict):
            err = err.get("message") or err.get("detail") or err
        return str(err)[:500]
    except Exception:
        return (resp.text or "")[:500]
