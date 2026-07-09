from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.services.agent import model_client
from app.services.agent import routing_policy
from app.services.agent.context_classifier import classify_context_need
from app.services.agent.grounding import log_grounding_check, log_router_pre_decision
from app.services.agent.models import RouteName, TurnRequest
from app.services.agent.research_utils import temporal_context

# Imported lazily to avoid circular imports — only used inside choose_research_level().
def _get_extract_named_comparison_subjects():  # type: ignore[misc]
    from app.services.agent.research_contracts import _extract_named_comparison_subjects  # noqa: PLC0415
    return _extract_named_comparison_subjects


def _get_count_comparison_dimensions():  # type: ignore[misc]
    from app.services.agent.research_contracts import _count_comparison_dimensions  # noqa: PLC0415
    return _count_comparison_dimensions

logger = logging.getLogger(__name__)


class OrchestratorDecision(BaseModel):
    route: RouteName
    confidence: float = Field(ge=0.0, le=1.0, default=0.6)
    reason: str = ""
    clarification_question: str | None = None
    output_format: str | None = None
    research_level: Literal["easy", "regular", "deep"] = "regular"
    requires_confirmation: bool = False
    confirmation_message: str | None = None
    fallback_research_level: Literal["easy", "regular"] = "regular"
    rewritten_request: str | None = None
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    source: str = "llm"
    available_routes: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None


SYSTEM_PROMPT = """You are Fronei's orchestration agent.

Choose exactly one route:
- direct: answer from general knowledge; no tool required.
- clarify: the user request is ambiguous, risky, under-specified, or missing a required target.
- research: the answer needs current, source-grounded, market, pricing, legal, financial, product, vendor, or time-sensitive information. This includes plainly-phrased questions about real-world wait times, processing times, scheduling backlogs, or "how long does X actually take in practice" — these require source-grounded evidence, not general knowledge.
- document: the user primarily wants Fronei to create/write/export a document/report/memo/deck artifact, and enough content is already provided.
- research_document: the user wants Fronei to create/write/export a document/report/memo/deck artifact and the contents need source-grounded research first.

Important distinction:
- If the user asks to research, find, compare, or look up tools/projects/repos that generate documents/decks/PPTs,
  the deliverable is a chat research answer, not a generated document. Choose research unless they explicitly ask
  Fronei to create/export the artifact.

Pending-intent rule:
- If last_turn_route="clarify" and the current message is a short answer (a location, name, date, or detail) that
  appears to answer the previous clarification question, choose the route the ORIGINAL request needed (usually
  research or research_document), not direct. The user is completing a prior intent, not starting a new one.
- If the user asks a meta question about the conversation itself (e.g. "are you researching now?", "did you find
  anything?", "what route did you pick?") choose direct — these never need source-grounded research.

Grounding rule:
- Never claim that prior/current conversation context contains an answer unless the supplied context actually includes
  prior turns. If no prior turns are present and the user asks a vague follow-up, choose clarify rather than direct.

Gap rule:
- If last_turn_had_gaps=true and the current message concerns the same subject as the prior turn, do not treat the
  prior turn's unresolved gap as a confirmed answer. Route to research again (or clarify if the prior gap's target
  needs narrowing) — an inability to confirm something is not evidence that it doesn't exist, especially for
  time-sensitive facts (schedules, prices, availability, current status) that can change or become discoverable
  with a different search approach. Do not answer direct by restating a prior gap as a settled negative.

If the route is research or research_document, also choose exactly one research_level:
- easy: very narrow freshness check or simple sourced lookup; minimal web use.
- regular: normal source-grounded research, comparison, recommendation, or briefing.
- deep: broad/high-stakes/document-grade investigation with many sources, verification, and repair.

Deep research is expensive and slower. Use deep only for explicit deep/comprehensive asks, high-stakes domains,
strategic/business/legal/financial/regulatory work, vendor/investment decisions, or broad document-grade research.
When research_level is deep, set requires_confirmation=true.

Return only compact JSON with this schema:
{
  "route": "direct|clarify|research|document|research_document",
  "confidence": 0.0-1.0,
  "reason": "short reason",
  "clarification_question": "required only for clarify",
  "output_format": "chat|markdown|docx|pptx|null",
  "research_level": "easy|regular|deep",
  "requires_confirmation": true|false,
  "confirmation_message": "required only when requires_confirmation is true",
  "fallback_research_level": "easy|regular",
  "rewritten_request": "optional clearer version of the user request"
}
"""


def decide(request: TurnRequest) -> OrchestratorDecision:
    return decide_with_options(
        request,
        available_routes=["direct", "clarify", "research", "document", "research_document"],
        available_tools=[],
    )


def decide_with_options(
    request: TurnRequest,
    *,
    available_routes: list[str],
    available_tools: list[str],
) -> OrchestratorDecision:
    if request.force_route:
        return _normalize_research_decision(request, OrchestratorDecision(
            route=request.force_route,
            confidence=1.0,
            reason="User explicitly forced the route.",
            output_format=request.output_format,
            source="forced",
            available_routes=available_routes,
            available_tools=available_tools,
        ))
    if request.comparison_mode:
        return _normalize_research_decision(request, OrchestratorDecision(
            route="research_document" if request.output_format != "chat" else "research",
            confidence=1.0,
            reason="Comparison matrix mode explicitly requires source-grounded research.",
            output_format=request.output_format,
            research_level=choose_research_level(request, "research"),
            source="guardrail",
            available_routes=available_routes,
            available_tools=available_tools,
        ))

    user_payload = json.dumps(
        {
            "message": request.message,
            **temporal_context(request.user_timezone),
            "prior_turn_context": request.prior_turn_context[-5000:] if request.prior_turn_context else "",
            "conversation_context": request.conversation_context[-5000:] if request.conversation_context else "",
            "last_turn_route": request.last_turn_route,
            "last_turn_had_gaps": request.last_turn_had_gaps,
            "quality_mode": request.quality_mode,
            "requested_research_level": request.research_level,
            "deep_research_confirmed": request.confirm_deep_research,
            "requested_output_format": request.output_format,
            "available_routes": available_routes,
            "available_tools": available_tools,
        },
        ensure_ascii=False,
    )
    context_decision = classify_context_need(request)
    log_router_pre_decision(
        logger,
        request=request,
        prompt=f"{SYSTEM_PROMPT}\n{user_payload}",
        router_name="orchestrator",
        available_routes=available_routes,
        available_tools=available_tools,
        context_intent=context_decision.intent,
        context_needs_context=context_decision.needs_context,
        context_target_scopes=context_decision.target_scopes,
        context_live_search=context_decision.live_search,
        context_reason=context_decision.reason,
        context_max_latency_ms=context_decision.budget.max_latency_ms,
    )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            role="orchestrator",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=400,
            timeout_s=18,
        )
        parsed = _parse_json(response.text)
        if parsed.get("route") in {"research", "research_document"} and not parsed.get("research_level"):
            parsed["research_level"] = choose_research_level(request, parsed["route"])
        decision = OrchestratorDecision.model_validate(parsed)
        decision.model_used = response.model_used
        decision.latency_ms = response.latency_ms
        decision.cost_usd = response.cost_usd
        decision.source = "llm"
        decision.available_routes = available_routes
        decision.available_tools = available_tools
        fabricated_claim = log_grounding_check(
            logger,
            request=request,
            router_name="orchestrator",
            decision=decision.route,
            reason=decision.reason,
            raw_decision=decision.route,
        )
        if fabricated_claim:
            if decision.route == "direct":
                decision.route = "clarify"
                decision.clarification_question = (
                    "I do not have prior conversation context for that. What topic should I use?"
                )
            decision.reason = (
                "The router claimed prior conversation context, but no prior turn context is available. "
                "Failing closed instead of relying on fabricated grounding."
            )
        decision = _normalize_research_decision(request, decision)
        if decision.route == "clarify" and not decision.clarification_question:
            decision.clarification_question = "Can you clarify what outcome you want and any constraints I should follow?"
        return decision
    except Exception as exc:
        logger.warning("agent orchestrator failed; using fallback route: %s", exc)
        fallback = heuristic_decide(request, available_routes=available_routes, available_tools=available_tools)
        fallback.reason = f"{fallback.reason} Orchestrator fallback after model failure."
        fallback.fallback_reason = str(exc)
        return fallback


def heuristic_decide(
    request: TurnRequest,
    *,
    available_routes: list[str] | None = None,
    available_tools: list[str] | None = None,
) -> OrchestratorDecision:
    available_routes = available_routes or ["direct", "clarify", "research", "document", "research_document"]
    available_tools = available_tools or []
    text = request.message.lower()
    asks_doc = _asks_for_document_artifact(text)
    asks_research = any(
        term in text
        for term in [
            "research",
            "sources",
            "current",
            "latest",
            "market",
            "compare",
            "benchmark",
            "recent",
            "citations",
        ]
    )
    if _looks_too_vague(text) and not _referent_resolves_from_context(request.message, request.conversation_context):
        return OrchestratorDecision(
            route="clarify",
            confidence=0.72,
            reason="The request is too vague to execute safely.",
            clarification_question="What topic or outcome should I focus on?",
            source="heuristic",
            available_routes=available_routes,
            available_tools=available_tools,
        )
    signal_decision = routing_policy.evaluate_routing_signals(request.message)
    if asks_research and asks_doc:
        route: RouteName = "research_document"
    elif asks_research:
        route = "research"
    elif asks_doc or request.output_format in {"docx", "markdown", "pptx"}:
        route = "document"
    elif signal_decision.suggested_route:
        route = "research"
    elif request.last_turn_had_gaps and _concerns_same_subject_as_prior_turn(request):
        # Same backstop as _normalize_research_decision's gap-rule override --
        # this path (heuristic fallback) doesn't run through that function, so
        # it needs its own copy rather than silently reintroducing the bug
        # whenever the orchestrator's LLM call fails.
        route = "research"
    else:
        route = "direct"
    research_level = choose_research_level(request, route, signal_decision)
    return OrchestratorDecision(
        route=route,
        confidence=0.64,
        reason="Deterministic fallback route based on request shape.",
        output_format=request.output_format,
        research_level=research_level,
        requires_confirmation=route in {"research", "research_document"} and research_level == "deep",
        confirmation_message=_deep_confirmation_message() if research_level == "deep" else None,
        fallback_research_level="regular",
        source="heuristic",
        available_routes=available_routes,
        available_tools=available_tools,
    )


def choose_research_level(
    request: TurnRequest,
    route: RouteName,
    signal_decision: routing_policy.RoutingSignalDecision | None = None,
) -> Literal["easy", "regular", "deep"]:
    if route not in {"research", "research_document"}:
        return "regular"
    if request.research_level in {"easy", "regular", "deep"}:
        return request.research_level  # type: ignore[return-value]
    text = request.message.lower()
    high_stakes_terms = [
        "legal",
        "regulatory",
        "compliance",
        "financial",
        "invest",
        "investment",
        "retirement",
        "vendor selection",
        "board",
        "ciso",
        "cio",
        "risk",
        "strategy",
        "market analysis",
        "operating model",
        "business model",
    ]
    deep_terms = [
        "deep",
        "comprehensive",
        "full analysis",
        "detailed report",
        "exhaustive",
        "thorough",
        "investment memo",
        "board-ready",
    ]
    easy_terms = ["quick", "briefly", "check", "current", "latest", "what is", "when is", "find out"]
    asks_doc = _asks_for_document_artifact(text)
    if any(term in text for term in deep_terms) or any(term in text for term in high_stakes_terms):
        return "deep"
    # Enumeration/count/list queries (routing_policy's enumeration_count_query signal
    # group) already escalate suggested_route to "agentic", but that never fed into
    # research_level, so it silently fell back to "regular" — not enough search breadth
    # for same-day multi-event lookups (e.g. sports schedules, multi-flight listings).
    # Reuses the existing signal evaluation rather than duplicating routing_policy's term
    # lists. Checked here — before the easy_terms fallback below — so a query like "how
    # many World Cup matches are there tomorrow" isn't pulled back down to "easy" just
    # because it also contains "current" or "what is".
    #
    # time_sensitive_factual (the "how long is" / "backlog" group directly above
    # enumeration_count_query in routing_policy.py) was checked for the same gap and does
    # NOT have it: its suggested_route is "web_fast", not "agentic" — it was deliberately
    # designed to stay on the lightweight path, not the fuller research runtime, so there
    # is no research_level to wire it into.
    if signal_decision is None:
        signal_decision = routing_policy.evaluate_routing_signals(request.message)
    if any(match.signal_group == "enumeration_count_query" for match in signal_decision.matched_signals):
        return "deep"
    # Phase 9 / Phase 11 — two independent structural signals, either alone → "deep".
    # Extract subjects and count dimensions once; both checks share the result.
    #
    # Signal A (Phase 9): ≥3 named subjects + recommendation/synthesis intent term.
    #   "Research the top 5 X… Provide a synthesized recommendation" must reach "deep"
    #   even without explicit keywords like "comprehensive" or "thorough".
    #
    # Signal B (Phase 11): ≥2 named subjects + ≥3 explicit comparison dimensions.
    #   "Compare AWS S3, Google Cloud Storage, and Azure Blob Storage on durability,
    #    pricing tiers, and egress costs" is inherently deep-shaped even when the user
    #   never asks "which one should I pick" — three subjects × three dimensions is
    #   the same research burden as any other deep comparison task.
    _synthesis_intent_terms = (
        "recommend", "recommendation", "which is best", "which would", "synthesiz",
        "best framework", "best option", "best choice", "best platform", "best tool",
    )
    try:
        _extract = _get_extract_named_comparison_subjects()
        _count_dims = _get_count_comparison_dimensions()
        named_subjects = _extract(request.message)
        # Signal A — recommendation-intent path (Phase 9)
        if len(named_subjects) >= 3 and any(term in text for term in _synthesis_intent_terms):
            return "deep"
        # Signal B — dimension-richness path (Phase 11)
        if len(named_subjects) >= 2 and _count_dims(request.message) >= 3:
            return "deep"
    except Exception:
        pass
    explicit_research = "research" in text
    if asks_doc and route == "research_document":
        return "regular"
    if not explicit_research and any(term in text for term in easy_terms) and len(text.split()) <= 18:
        return "easy"
    return "regular"


def _normalize_research_decision(request: TurnRequest, decision: OrchestratorDecision) -> OrchestratorDecision:
    text = request.message.lower()
    asks_research = "research" in text or decision.route in {"research", "research_document"}
    asks_doc = _asks_for_document_artifact(text)
    signal_decision = routing_policy.evaluate_routing_signals(request.message)
    # Only let routing signals override "direct" when the LLM was uncertain
    # (confidence < 0.85). A confident direct decision — e.g. "are you
    # researching now?" scored at 1.0 — must not be overridden by a keyword
    # match on "researching". The signal is correct context for uncertain cases;
    # it's noise when the model already knows the answer doesn't need a source.
    if (
        decision.source != "forced"
        and decision.route == "direct"
        and decision.confidence < 0.85
        and signal_decision.suggested_route
    ):
        decision.route = "research"
        decision.reason = f"Routing signals require source-grounded handling. {decision.reason}".strip()
        asks_research = True
    # Pending-intent carry-forward: if the previous turn was clarify (the model
    # asked a clarification question) and the current message is a short answer
    # (≤ 25 words, no new verb-led intent), the user is answering that question,
    # not starting a new conversation. Route to research rather than direct so
    # the original research intent completes instead of stalling at acknowledge.
    if (
        decision.source != "forced"
        and decision.route == "direct"
        and request.last_turn_route == "clarify"
        and len(request.message.split()) <= 25
        and not _asks_for_document_artifact(text)
    ):
        decision.route = "research"
        decision.reason = f"Resolving prior clarification; continuing with research. {decision.reason}".strip()
        asks_research = True
    # Gap-rule backstop: if the prior turn's research left an unresolved gap, a
    # "direct" decision now can't actually verify anything new -- it can only
    # restate the prior gap, confidently or not, as if it were a settled
    # negative. Unlike the low-confidence override above, this fires
    # regardless of confidence, because the live trace that motivated this
    # rule had confidence 1.0. Gated on _concerns_same_subject_as_prior_turn
    # rather than firing unconditionally, so an unrelated new topic right
    # after a gapped turn isn't needlessly forced into research.
    if (
        decision.source != "forced"
        and decision.route == "direct"
        and request.last_turn_had_gaps
        and _concerns_same_subject_as_prior_turn(request)
    ):
        decision.route = "research"
        decision.reason = (
            f"Prior turn had an unresolved research gap; re-researching rather than treating it as confirmed. {decision.reason}"
        ).strip()
        asks_research = True
    if request.output_format == "chat" and _asks_research_about_document_tools(text):
        decision.route = "research"
        decision.output_format = "chat"
        asks_doc = False
        asks_research = True
    if decision.route == "research" and asks_research and asks_doc:
        decision.route = "research_document"
        decision.output_format = decision.output_format or request.output_format
    if request.research_level in {"easy", "regular", "deep"}:
        decision.research_level = request.research_level  # type: ignore[assignment]
    elif decision.route in {"research", "research_document"}:
        decision.research_level = choose_research_level(request, decision.route, signal_decision)
    else:
        decision.research_level = "regular"
    if decision.route in {"research", "research_document"} and decision.research_level == "deep":
        decision.requires_confirmation = True
        decision.confirmation_message = decision.confirmation_message or _deep_confirmation_message()
    elif decision.research_level != "deep":
        decision.requires_confirmation = False
        decision.confirmation_message = None
    return decision


def _deep_confirmation_message() -> str:
    return (
        "This looks like deep research because it needs broader source coverage, evidence checking, "
        "and synthesis. I can run it in deep mode, which may take a few minutes. Continue with deep "
        "research, use regular research instead, or answer directly?"
    )


def _parse_json(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


_REFERENTIAL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bthe (\w+) one\b", re.IGNORECASE),   # "the Salesforce one"
    re.compile(r"\bthat\b", re.IGNORECASE),              # "can you go deeper on that"
    re.compile(r"\bthis\b", re.IGNORECASE),              # "what about this"
    re.compile(r"\bit\b", re.IGNORECASE),                # "how does it work"
    re.compile(r"\bsame\b", re.IGNORECASE),              # "same question for Azure"
)


def _referent_resolves_from_context(query: str, conversation_context: str) -> bool:
    """Return True if an apparently vague/referential query has enough
    prior-conversation context that proceeding to research is safer than
    asking the user to clarify again.

    Reads `request.conversation_context` (the string already on TurnRequest)
    rather than a `prior_context` list — TurnRequest has no such attribute;
    the eval harness formats prior_context turns into conversation_context
    before dispatch. Intentionally conservative: >50 chars of context is the
    only bar, which skips the clarify branch when any reasonable context
    exists. The failure cost of false-clarifying (friction, broken flow) is
    higher than the failure cost of occasionally misresolving a referent.
    """
    if not conversation_context or not conversation_context.strip():
        return False
    # Check if the query even contains a referential expression worth resolving
    query_lower = query.lower()
    has_referent = any(p.search(query_lower) for p in _REFERENTIAL_PATTERNS)
    if not has_referent:
        return False
    return len(conversation_context.strip()) > 50


_SAME_SUBJECT_MAX_WORDS = 12


def _concerns_same_subject_as_prior_turn(request: TurnRequest) -> bool:
    """Lightweight proxy for "the current message is continuing the prior
    turn's topic, not starting an unrelated new one." Used to gate the gap-rule
    backstop so an unrelated question right after a gapped turn isn't needlessly
    forced into research. Reuses _referent_resolves_from_context (a referential
    word like "that"/"it"/"same" plus real prior context) rather than inventing
    new detection logic; a short message is the other common shape a same-topic
    follow-up takes (a genuinely new, self-contained question tends to be
    longer). Intentionally conservative in favor of over-firing: an unnecessary
    research pass is a far safer failure mode than confidently restating a
    false negative as fact.
    """
    if _referent_resolves_from_context(request.message, request.conversation_context):
        return True
    return len(request.message.split()) <= _SAME_SUBJECT_MAX_WORDS


def _looks_too_vague(text: str) -> bool:
    words = [w for w in re.split(r"\W+", text) if w]
    if len(words) <= 2 and any(w in {"it", "this", "that", "them", "better", "fix", "research"} for w in words):
        return True
    return text.strip() in {"help", "do it", "make it better", "research it", "create it"}


def _asks_for_document_artifact(text: str) -> bool:
    if _asks_research_about_document_tools(text):
        return False
    artifact_terms = r"(?:document|report|docx|memo|briefing|deck|pptx?|slides?|presentation|powerpoint)"
    explicit_create = (
        rf"\b(?:create|make|generate|write|draft|build|compose|produce|export|download|turn|convert)\b"
        rf".{{0,80}}\b{artifact_terms}\b"
    )
    artifact_first = (
        rf"\b{artifact_terms}\b"
        rf".{{0,80}}\b(?:for me|from this|from the brief|from a brief|using the template|using templates|with the template|as a file|downloadable)\b"
    )
    return bool(re.search(explicit_create, text) or re.search(artifact_first, text))


def _asks_research_about_document_tools(text: str) -> bool:
    research_terms = (
        "look in",
        "look up",
        "find",
        "search github",
        "github",
        "research",
        "compare",
        "see if",
        "are there",
        "recent projects",
        "open-source projects",
        "projects",
        "github repos",
        "repositories",
        "tools",
        "alternatives",
    )
    artifact_tool_terms = (
        "generate ppt",
        "generate ppts",
        "generate pptx",
        "generate slides",
        "generate presentations",
        "generate decks",
        "ppt generator",
        "pptx generator",
        "slide generator",
        "presentation generator",
        "deck generator",
    )
    return any(term in text for term in research_terms) and any(term in text for term in artifact_tool_terms)
