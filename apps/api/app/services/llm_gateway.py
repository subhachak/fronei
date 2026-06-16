import logging
import os
import time
from dataclasses import dataclass, field
from typing import Generator

logger = logging.getLogger(__name__)

from litellm import completion, completion_cost

from app.config import get_settings
from app.schemas import RouteDecision
from app.services.prompts import (
    DEEP_RESEARCH_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
    WEB_CONTEXT_PROMPT,
    WORKER_SYSTEM_PROMPT,
)

MAX_COMPLETION_TOKENS = 8192
DEEP_RESEARCH_MAX_COMPLETION_TOKENS = 16384

# Hard caps so a stalled provider connection can't hang the SSE stream forever.
STREAM_REQUEST_TIMEOUT_S = 120   # overall time allowed for the HTTP call to complete
STREAM_CHUNK_TIMEOUT_S = 45      # max gap allowed between successive chunks

# Hard cap for non-streaming completion() calls (invoke_llm/_call_model,
# synthesize_answers, etc.). Without this, a stalled provider connection on a
# structured-output call (e.g. the AgentDeck v2 planner) can hang indefinitely
# -- the only backstop is the multi-hour document-pipeline timeout, leaving a
# turn idle with zero progress for many minutes. 180s is generous enough for
# large structured JSON completions (MAX_COMPLETION_TOKENS=8192) while still
# bounding the wait so the model-fallback chain in invoke_llm can kick in.
NON_STREAM_REQUEST_TIMEOUT_S = 180
# Keep only recent turns — older context is now covered by the running_summary
# injected via planner_context, so sending 20+ raw messages is wasteful.
MAX_HISTORY_MESSAGES = 8

# Gemini 2.0+ native Google Search grounding tool
_GEMINI_SEARCH_TOOL = [{"googleSearch": {}}]


@dataclass
class LLMResult:
    answer: str
    model_used: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_cost_usd: float | None
    fallback_errors: list[str] = field(default_factory=list)  # non-empty when fallback was used


# ── Provider setup ────────────────────────────────────────────────────────────
# configure_provider_keys() must be called once at application startup (via the
# lifespan hook in main.py). Do not call it per-request or from threads.

def configure_provider_keys() -> None:
    import litellm as _litellm
    s = get_settings()
    if s.openai_api_key:
        os.environ["OPENAI_API_KEY"] = s.openai_api_key
    if s.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = s.anthropic_api_key
    if s.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = s.gemini_api_key
    if s.openrouter_api_key:
        os.environ["OPENROUTER_API_KEY"] = s.openrouter_api_key
    # Silently drop params that specific models don't support (e.g. temperature
    # for o3/o1-series and claude-opus-4-8, tools for non-Gemini models, etc.)
    _litellm.drop_params = True


# Cheapest/fastest model per provider, used only for the admin "test connection" ping.
PROVIDER_TEST_MODELS = {
    "OpenAI": "gpt-4.1-mini",
    "Anthropic": "claude-haiku-4-5-20251001",
    "Gemini": "gemini/gemini-2.5-flash",
    "OpenRouter": "openrouter/deepseek/deepseek-chat",
}

# Maps a model identifier prefix (as stored in selected_model/model_used) to the
# admin-facing provider name. Shared by the circuit breaker and the admin Providers tab.
PROVIDER_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "OpenAI": ("gpt", "o1", "o3", "o4"),
    "Anthropic": ("claude",),
    "Gemini": ("gemini/", "gemini-"),
    "OpenRouter": ("openrouter/",),
}


def provider_for_model(model: str | None) -> str:
    if not model:
        return "unknown"
    for provider, prefixes in PROVIDER_MODEL_PREFIXES.items():
        if model.startswith(prefixes):
            return provider
    return "Other"


# ── Circuit breaker ───────────────────────────────────────────────────────────
# In-memory, per-process. After CIRCUIT_FAILURE_THRESHOLD consecutive failures for
# a provider, that provider's models are skipped in fallback chains for
# CIRCUIT_COOLDOWN_SECONDS (a "half-open" retry is allowed once the cooldown elapses).
# This avoids burning latency retrying a provider that is fully down on every request.
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60

_circuit_state: dict[str, dict] = {}


def _provider_circuit_open(provider: str) -> bool:
    state = _circuit_state.get(provider)
    if not state or state["failures"] < CIRCUIT_FAILURE_THRESHOLD:
        return False
    if time.time() - state["opened_at"] >= CIRCUIT_COOLDOWN_SECONDS:
        return False  # cooldown elapsed — half-open, allow a retry
    return True


def record_provider_success(provider: str) -> None:
    _circuit_state.pop(provider, None)


def record_provider_failure(provider: str) -> None:
    state = _circuit_state.setdefault(provider, {"failures": 0, "opened_at": 0.0})
    state["failures"] += 1
    if state["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        state["opened_at"] = time.time()


def _order_by_circuit(models: list[str]) -> list[str]:
    """Filter out models whose provider's circuit is open. If that would leave
    nothing to try, fall back to the original order (better to attempt a likely-
    failing call than to fail with no attempt at all)."""
    available = [m for m in models if not _provider_circuit_open(provider_for_model(m))]
    return available if available else models


def get_circuit_status() -> dict[str, dict]:
    """Snapshot of circuit-breaker state per provider, for the admin Providers tab."""
    now = time.time()
    status: dict[str, dict] = {}
    for provider, state in _circuit_state.items():
        open_ = state["failures"] >= CIRCUIT_FAILURE_THRESHOLD and (
            now - state["opened_at"] < CIRCUIT_COOLDOWN_SECONDS
        )
        status[provider] = {
            "consecutive_failures": state["failures"],
            "open": open_,
            "cooldown_remaining_s": (
                max(0, int(CIRCUIT_COOLDOWN_SECONDS - (now - state["opened_at"]))) if open_ else 0
            ),
        }
    return status


def test_provider_connection(provider: str) -> dict:
    """Make a minimal (1-token) completion call to verify a provider key is live.

    Used by the admin Providers tab. Costs a negligible fraction of a cent.
    """
    model = PROVIDER_TEST_MODELS.get(provider)
    if not model:
        return {"success": False, "error": f"No test model configured for provider '{provider}'."}
    started = time.perf_counter()
    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with one word: pong"}],
            max_tokens=5,
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = (response.choices[0].message.content or "").strip()
        return {"success": True, "model": model, "latency_ms": latency_ms, "response": text}
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"success": False, "model": model, "latency_ms": latency_ms, "error": str(exc)[:500]}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_gemini(model: str) -> bool:
    return model.startswith("gemini/")


# Models that reject a `temperature` param outright (litellm's drop_params
# doesn't catch these because its static model map doesn't recognize these
# model strings as unsupported). Anthropic's opus-4.x line errors with
# "temperature is deprecated for this model" if temperature is sent at all.
_NO_TEMPERATURE_PREFIXES: tuple[str, ...] = ("claude-opus-4",)


def _supports_temperature(model: str) -> bool:
    return not model.startswith(_NO_TEMPERATURE_PREFIXES)


def _extract_usage(response) -> tuple[int | None, int | None, float | None]:
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    cost = None
    try:
        cost = float(completion_cost(completion_response=response))
    except Exception:
        pass
    return prompt_tokens, completion_tokens, cost


def _build_messages(
    message: str,
    history: list[dict],
    deep_research: bool,
    web_context: str | None,
    planner_context: str | None,
    doc_context: str | None = None,
    artifact_context: str | None = None,
    system_prompt: str | None = None,
) -> list[dict]:
    sys_content = system_prompt or (DEEP_RESEARCH_SYSTEM_PROMPT if deep_research else WORKER_SYSTEM_PROMPT)
    if planner_context:
        sys_content = f"{sys_content}\n\nCONVERSATION CONTEXT:\n{planner_context}"

    msgs: list[dict] = [{"role": "system", "content": sys_content}]
    msgs.extend(history[-MAX_HISTORY_MESSAGES:])
    if artifact_context:
        # Artifact format instructions take precedence — inject before other context
        msgs.append({"role": "system", "content": artifact_context})
    if web_context:
        msgs.append({"role": "system", "content": f"{WEB_CONTEXT_PROMPT}\n\n{web_context}"})
    if doc_context:
        msgs.append({
            "role": "system",
            "content": (
                "The user has attached the following document. "
                "Use it to answer their question:\n\n"
                + doc_context
            ),
        })
    msgs.append({"role": "user", "content": message})
    return msgs


def _call_model(
    model: str,
    msgs: list[dict],
    max_tokens: int,
    enable_native_search: bool,
    request_timeout_s: float | None = None,
) -> object:
    """Call the model, retrying Gemini without grounding if the tool is rejected."""
    kwargs: dict = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "timeout": request_timeout_s or NON_STREAM_REQUEST_TIMEOUT_S,
    }
    if _supports_temperature(model):
        kwargs["temperature"] = 0.2
    if _is_gemini(model) and enable_native_search:
        kwargs["tools"] = _GEMINI_SEARCH_TOOL

    try:
        return completion(**kwargs)
    except Exception as exc:
        if _is_gemini(model) and "tools" in kwargs:
            kwargs.pop("tools")
            return completion(**kwargs)  # raises if still failing
        raise exc


# ── Streaming helpers ─────────────────────────────────────────────────────────

def _iter_with_stall_timeout(iterable, timeout_s: float) -> Generator:
    """Wrap a blocking iterator so it raises TimeoutError if no item arrives
    within `timeout_s`, instead of hanging forever on a stalled connection.

    Runs the underlying iteration in a background thread and relays items
    through a queue, so we can bound the wait on each `next()` call.
    """
    import queue
    import threading

    _SENTINEL = object()
    q: "queue.Queue" = queue.Queue(maxsize=8)

    def _worker() -> None:
        try:
            for item in iterable:
                q.put((item, None))
            q.put(_SENTINEL)
        except Exception as exc:  # noqa: BLE001
            q.put((None, exc))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while True:
        try:
            item = q.get(timeout=timeout_s)
        except queue.Empty:
            raise TimeoutError(f"LLM stream stalled for more than {timeout_s}s")
        if item is _SENTINEL:
            return
        value, exc = item
        if exc is not None:
            raise exc
        yield value

def _stream_call(model: str, msgs: list[dict], max_tokens: int, enable_native_search: bool):
    """completion() with stream=True, with Gemini grounding fallback."""
    kwargs: dict = {"model": model, "messages": msgs,
                    "max_tokens": max_tokens, "stream": True,
                    "timeout": STREAM_REQUEST_TIMEOUT_S}
    if _supports_temperature(model):
        kwargs["temperature"] = 0.2
    if _is_gemini(model) and enable_native_search:
        kwargs["tools"] = _GEMINI_SEARCH_TOOL
    try:
        return completion(**kwargs)
    except Exception as exc:
        if _is_gemini(model) and "tools" in kwargs:
            kwargs.pop("tools")
            return completion(**kwargs)
        raise exc


def _stream_models(
    models: list[str],
    msgs: list[dict],
    max_tokens: int,
    enable_native_search: bool = False,
) -> Generator:
    """
    Try models in order. Yields str tokens then a final LLMResult sentinel.
    Consumers distinguish items with isinstance(item, str) / isinstance(item, LLMResult).
    """
    started = time.perf_counter()
    for model in _order_by_circuit(models):
        provider = provider_for_model(model)
        try:
            response = _stream_call(model, msgs, max_tokens, enable_native_search)
        except Exception:
            record_provider_failure(provider)
            continue

        chunks: list = []
        full_text = ""
        stream_ok = True
        try:
            for chunk in _iter_with_stall_timeout(response, STREAM_CHUNK_TIMEOUT_S):
                chunks.append(chunk)
                token = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                if token:
                    full_text += token
                    yield token
        except Exception:
            stream_ok = False

        if not stream_ok:
            record_provider_failure(provider)
            if full_text:
                # Tokens were already streamed to the client for this model;
                # retrying with a fallback would duplicate/garble output, so
                # surface the stall as an error instead of hanging silently.
                raise RuntimeError("LLM stream stalled after partial output.")
            continue

        record_provider_success(provider)

        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens = completion_tokens_val = cost = None
        try:
            from litellm import stream_chunk_builder
            built = stream_chunk_builder(chunks, messages=msgs)
            usage = getattr(built, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens_val = getattr(usage, "completion_tokens", None)
            cost = float(completion_cost(completion_response=built))
        except Exception:
            pass

        yield LLMResult(
            answer=full_text, model_used=model, latency_ms=latency_ms,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens_val,
            estimated_cost_usd=cost,
        )
        return

    raise RuntimeError("All models failed during streaming.")


def stream_llm(
    message: str,
    route: RouteDecision,
    history: list[dict] | None = None,
    deep_research: bool = False,
    web_context: str | None = None,
    enable_native_search: bool = False,
    planner_context: str | None = None,
    doc_context: str | None = None,
    artifact_context: str | None = None,
) -> Generator:
    """Streaming variant of invoke_llm. Yields str tokens then a final LLMResult."""
    msgs = _build_messages(message, history or [], deep_research, web_context, planner_context, doc_context, artifact_context)
    max_tokens = DEEP_RESEARCH_MAX_COMPLETION_TOKENS if deep_research else MAX_COMPLETION_TOKENS
    yield from _stream_models(
        [route.primary_model, *route.fallbacks], msgs, max_tokens, enable_native_search
    )


def stream_synthesis(
    intent: str,
    sub_results: list[tuple[str, str]],
    route: RouteDecision,
) -> Generator:
    """Streaming variant of synthesize_answers."""
    parts = "\n\n".join(f"Sub-question: {q}\nAnswer:\n{a}" for q, a in sub_results)
    msgs = [
        {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"User intent: {intent}\n\n{parts}"},
    ]
    yield from _stream_models(
        [route.primary_model, *route.fallbacks], msgs, DEEP_RESEARCH_MAX_COMPLETION_TOKENS
    )


# ── Public API ────────────────────────────────────────────────────────────────

def invoke_llm(
    message: str,
    route: RouteDecision,
    history: list[dict] | None = None,
    deep_research: bool = False,
    web_context: str | None = None,
    enable_native_search: bool = False,
    planner_context: str | None = None,
    doc_context: str | None = None,
    artifact_context: str | None = None,
    request_timeout_s: float | None = None,
    max_tokens_override: int | None = None,
    system_prompt: str | None = None,
) -> LLMResult:
    msgs = _build_messages(
        message,
        history or [],
        deep_research,
        web_context,
        planner_context,
        doc_context,
        artifact_context,
        system_prompt,
    )
    max_tokens = max_tokens_override or (DEEP_RESEARCH_MAX_COMPLETION_TOKENS if deep_research else MAX_COMPLETION_TOKENS)
    models_to_try = _order_by_circuit([route.primary_model, *route.fallbacks])
    errors: list[str] = []
    started = time.perf_counter()

    for model in models_to_try:
        provider = provider_for_model(model)
        try:
            response = _call_model(model, msgs, max_tokens, enable_native_search, request_timeout_s)
        except Exception as exc:
            err = f"{model}: {exc}"
            errors.append(err)
            logger.warning("Model fallback: %s", err)
            record_provider_failure(provider)
            continue

        record_provider_success(provider)
        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens, completion_tokens, cost = _extract_usage(response)
        return LLMResult(
            answer=response.choices[0].message.content or "",
            model_used=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            fallback_errors=errors,  # empty when no fallback, populated when fallback occurred
        )

    raise RuntimeError("All models failed. " + " | ".join(errors))


def invoke_llm_json(
    messages: list[dict],
    route: RouteDecision,
) -> LLMResult:
    """Non-streaming LLM call for short JSON routing decisions."""

    models_to_try = _order_by_circuit([route.primary_model, *route.fallbacks])
    errors: list[str] = []
    started = time.perf_counter()

    for model in models_to_try:
        provider = provider_for_model(model)
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": 1024,
            "timeout": NON_STREAM_REQUEST_TIMEOUT_S,
            "response_format": {"type": "json_object"},
        }
        if _supports_temperature(model):
            kwargs["temperature"] = 0.0

        try:
            try:
                response = completion(**kwargs)
            except Exception as exc:
                logger.debug("response_format rejected by provider, retrying plain JSON call: %s", exc)
                kwargs.pop("response_format", None)
                response = completion(**kwargs)
        except Exception as exc:
            err = f"{model}: {exc}"
            errors.append(err)
            logger.warning("Model fallback (json): %s", err)
            record_provider_failure(provider)
            continue

        record_provider_success(provider)
        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens, completion_tokens, cost = _extract_usage(response)
        return LLMResult(
            answer=response.choices[0].message.content or "",
            model_used=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            fallback_errors=errors,
        )

    raise RuntimeError("All models failed (json). " + " | ".join(errors))


def synthesize_answers(
    intent: str,
    sub_results: list[tuple[str, str]],
    route: RouteDecision,
) -> LLMResult:
    """Combines multiple sub-query answers into one coherent response."""
    parts = "\n\n".join(f"Sub-question: {q}\nAnswer:\n{a}" for q, a in sub_results)
    msgs = [
        {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"User intent: {intent}\n\n{parts}"},
    ]
    models_to_try = _order_by_circuit([route.primary_model, *route.fallbacks])
    errors: list[str] = []
    started = time.perf_counter()

    for model in models_to_try:
        provider = provider_for_model(model)
        try:
            synth_kwargs: dict = {
                "model": model,
                "messages": msgs,
                "max_tokens": DEEP_RESEARCH_MAX_COMPLETION_TOKENS,
                "timeout": NON_STREAM_REQUEST_TIMEOUT_S,
            }
            if _supports_temperature(model):
                synth_kwargs["temperature"] = 0.2
            response = completion(**synth_kwargs)
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            record_provider_failure(provider)
            continue

        record_provider_success(provider)
        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens, completion_tokens, cost = _extract_usage(response)
        return LLMResult(
            answer=response.choices[0].message.content or "",
            model_used=model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
        )

    raise RuntimeError("Synthesis failed. " + " | ".join(errors))
