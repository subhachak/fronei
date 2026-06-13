"""
Agentic pre-processor that analyses the user's query + conversation history
and returns a Plan that the worker pipeline executes.

The planner uses an LLM to:
  - Understand true intent (including references to prior context)
  - Summarise only the relevant history so the worker doesn't wade through everything
  - Decide whether web search is needed and craft an optimised search query
  - Decompose multi-part questions into independent sub-queries
  - Override the keyword-based task/complexity classifier with something smarter

On any failure (LLM error, bad JSON) it returns a passthrough plan so the rest
of the pipeline continues unchanged.
"""
import json
import re
import time
from dataclasses import dataclass

from litellm import completion, completion_cost

from app.services.prompts import PLANNER_SYSTEM_PROMPT

# Planner sees last 6 raw turns; older context arrives via running_summary injection
MAX_HISTORY_FOR_PLANNER = 6
# Fallback model if the configured planner model fails
_PLANNER_FALLBACK_MODEL = "gemini/gemini-2.5-flash"


@dataclass
class SubQuery:
    query: str
    purpose: str
    task_type: str | None       # task type for per-sub-query routing
    preferred_model: str | None # model hint for per-sub-query routing


@dataclass
class Plan:
    # Turn classification (new in step 1)
    turn_type: str              # new_task | continuation | correction | constraint_change | follow_up
    action: str                 # answer_directly | use_workers | decompose
    # Content
    intent: str
    context_summary: str
    enriched_prompt: str
    needs_web_search: bool
    web_search_criticality: str  # trivial | material
    search_query: str | None
    preferred_model: str | None # model hint for top-level routing
    sub_queries: list[SubQuery]
    task_type: str | None       # overrides keyword classifier when set
    complexity: str | None      # overrides keyword classifier when set
    recommend_deep_research: bool
    research_reason: str
    research_risk_factors: list[str]
    research_confidence: str
    # Unified plan additions (see docs/unified-plan-architecture.md)
    wants_document_output: bool
    document_brief: dict             # doc_type/title/audience/tone/length, values may be None
    document_format_options: list[str]
    document_format_recommendation: str | None
    plan_confidence: str             # low | medium | high
    open_questions: list[str]
    # Planner metadata
    planner_model: str
    planner_latency_ms: int
    planner_cost_usd: float


def passthrough(message: str) -> Plan:
    """No-op plan — passes the original message through unchanged."""
    return Plan(
        turn_type="new_task",
        action="use_workers",
        intent="",
        context_summary="",
        enriched_prompt=message,
        needs_web_search=False,
        web_search_criticality="material",
        search_query=None,
        preferred_model=None,
        sub_queries=[],
        task_type=None,
        complexity=None,
        recommend_deep_research=False,
        research_reason="",
        research_risk_factors=[],
        research_confidence="low",
        wants_document_output=False,
        document_brief={},
        document_format_options=[],
        document_format_recommendation=None,
        plan_confidence="low",
        open_questions=[],
        planner_model="none",
        planner_latency_ms=0,
        planner_cost_usd=0.0,
    )


def _parse_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Model may have wrapped JSON in markdown fences — extract it
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


_VALID_TURN_TYPES = {"new_task", "continuation", "correction", "constraint_change", "follow_up"}
_VALID_ACTIONS    = {"answer_directly", "use_workers", "decompose"}
_VALID_CRITICALITIES = {"trivial", "material"}
_VALID_CONFIDENCE = {"low", "medium", "high"}
_VALID_DOC_TYPES = {
    "executive_report", "proposal", "memo", "technical_spec",
    "meeting_notes", "one_pager", "letter", "resume", None,
}
_VALID_DOCUMENT_FORMATS = {"markdown", "docx", "pptx", "pdf", "xlsx"}


def _build_plan(data: dict, message: str, model: str, latency_ms: int, cost: float) -> Plan:
    sub_queries = [
        SubQuery(
            query=sq["query"],
            purpose=sq.get("purpose", ""),
            task_type=sq.get("task_type") or None,
            preferred_model=sq.get("preferred_model") or None,
        )
        for sq in (data.get("sub_queries") or [])
        if isinstance(sq, dict) and sq.get("query")
    ]
    raw_turn_type = data.get("turn_type") or "new_task"
    raw_action    = data.get("action") or "use_workers"

    raw_criticality = data.get("web_search_criticality") or "material"
    web_search_criticality = raw_criticality if raw_criticality in _VALID_CRITICALITIES else "material"

    raw_brief = data.get("document_brief") or {}
    document_brief = {
        field: (raw_brief.get(field) or None)
        for field in ("doc_type", "title", "audience", "tone", "length")
    } if isinstance(raw_brief, dict) else {}
    if document_brief.get("doc_type") not in _VALID_DOC_TYPES:
        document_brief["doc_type"] = None

    document_format_options = [
        str(x) for x in (data.get("document_format_options") or [])
        if isinstance(x, str) and x in _VALID_DOCUMENT_FORMATS
    ]
    raw_format_rec = data.get("document_format_recommendation")
    document_format_recommendation = raw_format_rec if raw_format_rec in _VALID_DOCUMENT_FORMATS else None

    raw_confidence = data.get("plan_confidence") or "low"
    plan_confidence = raw_confidence if raw_confidence in _VALID_CONFIDENCE else "low"

    return Plan(
        turn_type=raw_turn_type if raw_turn_type in _VALID_TURN_TYPES else "new_task",
        action=raw_action if raw_action in _VALID_ACTIONS else "use_workers",
        intent=data.get("intent", ""),
        context_summary=data.get("context_summary", ""),
        enriched_prompt=data.get("enriched_prompt") or message,
        needs_web_search=bool(data.get("needs_web_search", False)),
        web_search_criticality=web_search_criticality,
        search_query=data.get("search_query") or None,
        preferred_model=data.get("preferred_model") or None,
        sub_queries=sub_queries,
        task_type=data.get("task_type") or None,
        complexity=data.get("complexity") or None,
        recommend_deep_research=bool(data.get("recommend_deep_research", False)),
        research_reason=data.get("research_reason") or "",
        research_risk_factors=[
            str(x) for x in (data.get("research_risk_factors") or [])
            if isinstance(x, str) and x.strip()
        ][:6],
        research_confidence=data.get("research_confidence") or "low",
        wants_document_output=bool(data.get("wants_document_output", False)),
        document_brief=document_brief,
        document_format_options=document_format_options,
        document_format_recommendation=document_format_recommendation,
        plan_confidence=plan_confidence,
        open_questions=[
            str(x) for x in (data.get("open_questions") or [])
            if isinstance(x, str) and x.strip()
        ][:6],
        planner_model=model,
        planner_latency_ms=latency_ms,
        planner_cost_usd=cost,
    )


def plan_to_dict(plan: Plan) -> dict:
    """Serialise a Plan for persistence on the user message row (plan_json)."""
    return {
        "turn_type": plan.turn_type,
        "action": plan.action,
        "intent": plan.intent,
        "context_summary": plan.context_summary,
        "enriched_prompt": plan.enriched_prompt,
        "needs_web_search": plan.needs_web_search,
        "web_search_criticality": plan.web_search_criticality,
        "search_query": plan.search_query,
        "preferred_model": plan.preferred_model,
        "sub_queries": [
            {
                "query": sq.query, "purpose": sq.purpose,
                "task_type": sq.task_type, "preferred_model": sq.preferred_model,
            }
            for sq in plan.sub_queries
        ],
        "task_type": plan.task_type,
        "complexity": plan.complexity,
        "recommend_deep_research": plan.recommend_deep_research,
        "research_reason": plan.research_reason,
        "research_risk_factors": plan.research_risk_factors,
        "research_confidence": plan.research_confidence,
        "wants_document_output": plan.wants_document_output,
        "document_brief": plan.document_brief,
        "document_format_options": plan.document_format_options,
        "document_format_recommendation": plan.document_format_recommendation,
        "plan_confidence": plan.plan_confidence,
        "open_questions": plan.open_questions,
        "planner_model": plan.planner_model,
        "planner_latency_ms": plan.planner_latency_ms,
        "planner_cost_usd": plan.planner_cost_usd,
    }


def plan_from_dict(data: dict, message: str = "") -> Plan:
    """Reconstruct a Plan from a previously persisted plan_json blob."""
    return _build_plan(
        data, message,
        data.get("planner_model", "none"),
        int(data.get("planner_latency_ms") or 0),
        float(data.get("planner_cost_usd") or 0.0),
    )


def apply_confirmed_plan(plan: Plan, confirmed: dict | None) -> Plan:
    """Apply user-confirmed overrides (from the plan_proposed popup) onto a Plan.

    Returns the same Plan instance, mutated in place, for convenience.
    """
    if not confirmed:
        return plan
    if confirmed.get("web_search") is not None:
        plan.needs_web_search = bool(confirmed["web_search"])
    if confirmed.get("deep_research") is not None:
        plan.recommend_deep_research = bool(confirmed["deep_research"])
    if confirmed.get("document") is not None:
        plan.wants_document_output = bool(confirmed["document"])
    if confirmed.get("document_format"):
        fmt = confirmed["document_format"]
        if fmt in _VALID_DOCUMENT_FORMATS:
            plan.document_format_recommendation = fmt
    if confirmed.get("document_brief"):
        brief = dict(plan.document_brief or {})
        for k, v in confirmed["document_brief"].items():
            if k in ("doc_type", "title", "audience", "tone", "length") and v:
                brief[k] = v
        plan.document_brief = brief
    return plan


def run_planner(
    message: str,
    history: list[dict],
    planner_model: str,
    running_summary: str = "",
    active_task: dict | None = None,
    user_memory: str = "",
    doc_context: str = "",
    user_hints: dict | None = None,
) -> Plan:
    """
    Runs the planner LLM and returns a Plan.
    Falls back gracefully to passthrough(message) on any error.

    running_summary: rolling plain-text summary of recent turns
    active_task: current task state dict (goal, constraints, steps)

    Caller must ensure provider env keys are set (via configure_provider_keys)
    before calling this function.
    """
    recent = history[-MAX_HISTORY_FOR_PLANNER:]
    msgs: list[dict] = [{"role": "system", "content": PLANNER_SYSTEM_PROMPT}]

    # Inject conversation state between system prompt and history so the planner
    # can classify the turn accurately and avoid re-deriving prior context.
    state_parts: list[str] = []
    if running_summary:
        state_parts.append(f"CONVERSATION SUMMARY (recent turns):\n{running_summary}")
    if active_task:
        state_parts.append(f"ACTIVE TASK:\n{json.dumps(active_task, indent=2)}")
    if user_memory:
        state_parts.append(f"USER MEMORY (persistent facts about this user):\n{user_memory}")
    if doc_context:
        preview = doc_context[:2000] + ("…" if len(doc_context) > 2000 else "")
        state_parts.append(
            f"ATTACHED DOCUMENT (preview — full text sent to worker):\n{preview}"
        )
    if user_hints:
        hint_lines: list[str] = []
        if user_hints.get("deep_research"):
            hint_lines.append(
                "- The user explicitly turned on 'Deep Research' mode for this turn via the "
                "composer. Treat this as a strong signal that recommend_deep_research should "
                "be true and research_confidence high, unless the request is trivial or purely "
                "conversational (in which case use your judgement and explain why in research_reason)."
            )
        if user_hints.get("document"):
            hint_lines.append(
                "- The user explicitly selected 'Document' output for this turn via the "
                "composer. Treat this as a strong signal that wants_document_output should be "
                "true. Infer document_brief fields (doc_type, audience, tone, length) and "
                "document_format_options/document_format_recommendation as confidently as "
                "possible from the request and conversation context — only leave a field null "
                "if it genuinely cannot be inferred."
            )
        if hint_lines:
            state_parts.append(
                "USER-PROVIDED HINTS (explicit signals from the composer — weigh heavily, "
                "but still apply judgement and your own reasoning):\n" + "\n".join(hint_lines)
            )

    if state_parts:
        msgs.append({"role": "system", "content": "\n\n".join(state_parts)})

    msgs.extend(recent)
    msgs.append({"role": "user", "content": message})

    # Deduplicated model list: configured planner → fallback
    seen: set[str] = set()
    models_to_try: list[str] = []
    for m in [planner_model, _PLANNER_FALLBACK_MODEL]:
        if m not in seen:
            seen.add(m)
            models_to_try.append(m)

    started = time.perf_counter()
    raw: str | None = None
    used_model = "none"
    planner_cost = 0.0

    for model in models_to_try:
        try:
            response = completion(
                model=model,
                messages=msgs,
                temperature=0.1,
                max_tokens=1024,
            )
            raw = response.choices[0].message.content or ""
            used_model = model
            try:
                planner_cost = float(completion_cost(completion_response=response))
            except Exception:
                planner_cost = 0.0
            break
        except Exception:
            continue

    latency_ms = int((time.perf_counter() - started) * 1000)

    if not raw:
        return passthrough(message)

    data = _parse_json(raw)
    if data is None:
        return passthrough(message)

    return _build_plan(data, message, used_model, latency_ms, planner_cost)
