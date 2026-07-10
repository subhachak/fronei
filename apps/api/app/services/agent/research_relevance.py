"""research_relevance.py — LLM-judged aggregate relevance gate for search results.

Distinct from research_utils._estimate_relevance() (a deterministic keyword-
overlap heuristic used per-source for ranking). This module answers a
different question: across ALL aggregated search_worker results, what
fraction plausibly relate to the research target at all? That's the signal
the relevance_gate LangGraph node uses to decide whether it's worth paying
for read/classify_claims/expand_source_graph/bind, or whether to retry with
a narrower query first, or give up and disclose the gap.

Responsibilities:
  - score_search_relevance: one LLM call judging aggregate relevance
  - reformulate_queries_for_exact_match: the one-retry query narrowing step
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.services.agent import model_client
from app.services.agent.models import Source
from app.services.agent.models import TurnRequest
from app.services.agent.prompt_library import resolve_prompt
from app.services.agent.research_models import SearchWorkerPlan
from app.services.agent.research_utils import _parse_json

logger = logging.getLogger(__name__)

# "<50% relevant" in the task spec that motivated this module.
RELEVANCE_THRESHOLD = 0.5

RELEVANCE_GATE_PROMPT = """You are the Fronei search-relevance gate.

You are given a target research entity/topic and a list of web search results
(title + snippet only, no full content). Judge what fraction of these results
plausibly relate to the target -- results a researcher would actually use to
answer questions about it, not just ones that share a word with it.

Return only JSON: {"relevance_fraction": 0.0-1.0, "reasoning": "one sentence"}

A result that matches an unrelated tool, product, or topic which happens to
share a name or word with the target does NOT count as relevant, even if it
superficially matches on search terms.
"""


@dataclass
class RelevanceAssessment:
    relevance_fraction: float
    reasoning: str
    model_calls_made: int
    cost_usd: float

    @property
    def sufficient(self) -> bool:
        return self.relevance_fraction >= RELEVANCE_THRESHOLD


def score_search_relevance(
    sources: list[Source],
    target: str,
    request: TurnRequest,
) -> RelevanceAssessment:
    """Judge what fraction of `sources` plausibly relate to `target`.

    Fails open (treats the batch as sufficient) on any error -- a broken judge
    should not silently downgrade every research run to "insufficient
    evidence" mode. Skips the model call entirely (no cost) when there's
    nothing to judge or no target to judge against.
    """
    if not sources:
        return RelevanceAssessment(0.0, "no search results to judge", 0, 0.0)
    if not target:
        return RelevanceAssessment(1.0, "no target entity available to judge against", 0, 0.0)
    try:
        prompt = resolve_prompt(
            "agent.research.relevance_gate.default",
            agent_id="relevance_gate",
            fallback_system_prompt=RELEVANCE_GATE_PROMPT,
            variables=["target", "results"],
        )
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "target": target,
                            "results": [
                                {"title": s.title, "snippet": s.snippet[:280]}
                                for s in sources[:40]
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            role="relevance_gate",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=300,
            timeout_s=15,
        )
        payload = _parse_json(response.text)
        fraction = max(0.0, min(1.0, float(payload.get("relevance_fraction", 1.0))))
        reasoning = str(payload.get("reasoning", ""))[:300]
        return RelevanceAssessment(fraction, reasoning, 1, response.cost_usd or 0.0)
    except Exception as exc:
        logger.warning("relevance_gate: scoring failed, treating batch as sufficient: %s", exc)
        return RelevanceAssessment(1.0, f"scoring failed ({exc}); treated as sufficient", 0, 0.0)


def reformulate_queries_for_exact_match(
    workers: list[SearchWorkerPlan],
    target: str,
) -> list[SearchWorkerPlan]:
    """The one-retry narrowing step: force exact-phrase matching on the
    target entity so the retry search can't drift onto an unrelated
    same-word topic the way the first pass did."""
    if not target:
        return workers
    quoted = f'"{target}"'
    reformulated: list[SearchWorkerPlan] = []
    for worker in workers:
        query = worker.query
        if quoted.lower() not in query.lower():
            query = f"{quoted} {query}".strip()[:220]
        reformulated.append(worker.model_copy(update={"query": query}))
    return reformulated


__all__ = [
    "RELEVANCE_GATE_PROMPT",
    "RELEVANCE_THRESHOLD",
    "RelevanceAssessment",
    "reformulate_queries_for_exact_match",
    "score_search_relevance",
]
