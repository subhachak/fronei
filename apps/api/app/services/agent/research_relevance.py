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
  - regenerate_queries_after_low_relevance: the one-retry query rewrite step
    (LLM-based, given the judge's failure reasoning; falls back to plain
    exact-phrase quoting on any failure)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import get_settings
from app.services.agent import model_client
from app.services.agent.models import Source
from app.services.agent.models import TurnRequest
from app.services.agent.prompt_library import resolve_prompt
from app.services.agent.research_models import SearchWorkerPlan
from app.services.agent.research_utils import _parse_json

logger = logging.getLogger(__name__)

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

QUERY_RETRY_PROMPT = """You are the Fronei search query author, retrying after a failed search.

The first search for this target returned results a relevance judge scored as
mostly irrelevant. You are given the original queries, the target entity/
topic, and the judge's explanation of why the results didn't match. Rewrite
each query to fix the problem.

Usually the fix is anchoring more tightly to the target's exact proper name
and dropping generic or jargon terms that could match an unrelated topic,
tool, or product entirely -- not just adding quotes around the same words.
If a query's own subject or wording looks like the actual defect (not just
"too broad"), rebuild it from the target name rather than lightly editing it.

Return only JSON: {"queries": [{"index": 0, "query": "<rewritten query>"}, ...]}

One entry per original query (by its 0-based index in the input list), in
any order. Do not skip an index.
"""


def relevance_threshold() -> float:
    """Minimum relevance_fraction to proceed past the gate. Reads Settings
    fresh on every call (not a frozen module constant) so it's tunable via
    the RELEVANCE_GATE_THRESHOLD env var without a code change or redeploy --
    e.g. to loosen/tighten it empirically after running against more queries."""
    return get_settings().relevance_gate_threshold


@dataclass
class RelevanceAssessment:
    relevance_fraction: float
    reasoning: str
    model_calls_made: int
    cost_usd: float

    @property
    def sufficient(self) -> bool:
        return self.relevance_fraction >= relevance_threshold()


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
    """Deterministic fallback narrowing step: force exact-phrase matching on
    the target entity. Cheap and guaranteed-safe, but only fixes an
    over-broad query -- it can't fix a query whose actual defect is a bad or
    incomplete target extraction, since it just quotes the same target
    string rather than rebuilding the query. Used when
    regenerate_queries_after_low_relevance()'s LLM call fails."""
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


def regenerate_queries_after_low_relevance(
    workers: list[SearchWorkerPlan],
    target: str,
    reasoning: str,
    request: TurnRequest,
) -> tuple[list[SearchWorkerPlan], int, float]:
    """The one-retry query rewrite step. Given the relevance judge's failure
    reasoning, asks the query_author role to rebuild each worker's query --
    this catches a genuinely bad/incomplete target extraction, not just an
    over-broad query, which reformulate_queries_for_exact_match() alone
    cannot fix (it only quote-wraps the same target string).

    Falls back to reformulate_queries_for_exact_match() on any failure
    (missing credentials, malformed JSON, a missing index in the response),
    so the retry never comes back empty-handed. Returns
    (workers, model_calls_made, cost_usd) so the caller can fold the retry's
    cost into its own budget accounting.
    """
    if not workers:
        return workers, 0, 0.0
    try:
        prompt = resolve_prompt(
            "agent.research.query_retry.default",
            agent_id="query_author",
            fallback_system_prompt=QUERY_RETRY_PROMPT,
            variables=["target", "reasoning", "queries"],
        )
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "target": target,
                            "reasoning": reasoning or "results did not plausibly relate to the target",
                            "queries": [{"index": i, "query": w.query} for i, w in enumerate(workers)],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            role="query_author",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=600,
            timeout_s=15,
        )
        payload = _parse_json(response.text)
        entries = payload.get("queries")
        if not isinstance(entries, list):
            raise ValueError("malformed query_retry response: missing 'queries' list")
        by_index: dict[int, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            query = str(entry.get("query", "")).strip()
            if isinstance(idx, int) and query:
                by_index[idx] = query[:220]
        regenerated = [
            worker.model_copy(update={"query": by_index[i]}) if i in by_index else worker
            for i, worker in enumerate(workers)
        ]
        return regenerated, 1, response.cost_usd or 0.0
    except Exception as exc:
        logger.warning("relevance_gate: query regeneration failed, falling back to exact-phrase retry: %s", exc)
        return reformulate_queries_for_exact_match(workers, target), 0, 0.0


__all__ = [
    "QUERY_RETRY_PROMPT",
    "RELEVANCE_GATE_PROMPT",
    "RelevanceAssessment",
    "reformulate_queries_for_exact_match",
    "regenerate_queries_after_low_relevance",
    "relevance_threshold",
    "score_search_relevance",
]
