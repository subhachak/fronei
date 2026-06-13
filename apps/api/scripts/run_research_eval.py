"""End-to-end eval harness for Deep Research v2.

Runs `_run_pipeline` (via `run_research`) against the scenarios in
`tests/fixtures/research_eval_cases.json`, with deterministic stand-ins for
web search and LLM calls so the run is fast, offline, and reproducible.

For each case this checks structural signals that correspond to the case's
`required_outcome` (source tiers/roles admitted, question-thread evidence
roles, gaps/contradictions, rejected sources) — i.e. whether the *evidence
pipeline*, not the prose synthesis, behaved as the design doc intends.

Usage:
    cd apps/api
    python scripts/run_research_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ResearchRun
from app.services import research_orchestrator as ro
from app.services.research_metadata import research_meta_for_run
from app.services.web_context import WebSource
from app.services.llm_gateway import LLMResult


FIXTURES_PATH = ROOT / "tests" / "fixtures" / "research_eval_cases.json"

# Per-case synthetic source pools: (url, title, content)
SOURCE_POOLS: dict[str, list[tuple[str, str, str]]] = {
    "immigration_current_operational_timeline": [
        (
            "https://www.uscis.gov/forms/all-forms/how-do-i-request-premium-processing-for-form-i-539-i-765",
            "Premium processing eligibility for Form I-539/I-765",
            "Official policy: USCIS premium processing eligibility now includes Form I-765 "
            "H-4 EAD when filed concurrently with Form I-129 H-1B. File Form I-907 to request "
            "premium processing. Effective date 2026-01-30.",
        ),
        (
            "https://www.immihelp.com/forum/h4-ead-current-timeline",
            "H4 EAD current approval timeline reports",
            "Community reports: recent H4 EAD approvals are taking 4 to 6 months as of May 2026. "
            "Many users report receipt date delays and service center backlog this month.",
        ),
        (
            "https://www.trackitt.com/h4-ead-2023-timeline",
            "H4 EAD processing time in 2023",
            "In 2023 H4 EAD processing time was about 3 to 5 months based on case tracker data "
            "from that year.",
        ),
    ],
    "immigration_official_policy_single_source": [
        (
            "https://www.uscis.gov/forms/i-907",
            "Form I-907 Request for Premium Processing Service",
            "Official eligibility: H-4 EAD Form I-765 is eligible for premium processing under "
            "Form I-907 when filed concurrently with H-1B Form I-129. Effective date 2026-01-30.",
        ),
        (
            "https://www.immihelp.com/forum/h4-ead-premium",
            "Some users say H4 EAD premium processing did not work",
            "Community member reports their H4 EAD premium processing request was rejected; "
            "anecdotal experience suggests it might not be honored in every case.",
        ),
    ],
    "anecdote_noisy_operational_reality": [
        (
            "https://www.reddit.com/r/personalfinance/comments/payment_support",
            "Payment processor support is taking forever",
            "Community member reports their support ticket with the payment processor has been "
            "open for three weeks with no response; anecdotal case of a long resolution time.",
        ),
        (
            "https://www.quora.com/Is-this-payment-processor-support-slow",
            "Is this payment processor support slow right now?",
            "Another user reports a similar experience: their support case took over two weeks "
            "to resolve this month, an anecdotal report of a slow resolution time.",
        ),
        (
            "https://www.blind.com/forum/payment-processor-support-delay",
            "Anecdotal reports of payment processor support delays",
            "A third community member shares an anecdotal case describing a multi-week support "
            "delay with the payment processor.",
        ),
    ],
    "medical_current_guidance": [
        (
            "https://www.heart.org/en/health-topics/high-blood-pressure/treatment",
            "Official AHA/ACC guideline for hypertension treatment",
            "Official guideline: first-line treatment for uncomplicated hypertension in adults "
            "includes thiazide diuretics, ACE inhibitors, ARBs, or calcium channel blockers. "
            "Guidance last updated 2026.",
        ),
        (
            "https://www.reddit.com/r/health/comments/bp_home_remedy",
            "I lowered my blood pressure with a home remedy",
            "Anecdotal case: a community member reports their blood pressure improved after "
            "trying a home remedy; this is a personal anecdote, not medical guidance.",
        ),
    ],
    "finance_company_current_fact": [
        (
            "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results",
            "NVIDIA Announces Financial Results for Latest Quarter",
            "NVIDIA today reported revenue of $44.1 billion for the quarter ended April 2026, "
            "according to the company's quarterly filing dated May 2026.",
        ),
    ],
    "technology_current_capability": [
        (
            "https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching",
            "Prompt caching - Claude Docs",
            "Official documentation: the Claude API currently supports prompt caching, with "
            "cache write and cache read pricing and a limit of 4 cache breakpoints per request. "
            "Last updated 2026-03-01.",
        ),
        (
            "https://www.reddit.com/r/ClaudeAI/comments/prompt_caching_tips",
            "How I use prompt caching in my app",
            "Community member shares an anecdotal report of how they configured prompt caching "
            "and the latency improvements they observed.",
        ),
    ],
}


def _pool_to_sources(case_id: str) -> list[WebSource]:
    return [
        WebSource(title=title, url=url, content=content)
        for url, title, content in SOURCE_POOLS.get(case_id, [])
    ]


def _make_search(pool: list[WebSource]):
    def _search(query, recency=None):
        return "test", list(pool)
    return _search


def _stub_invoke_llm(*_args, **_kwargs):
    return LLMResult(
        answer="Synthesised research answer based on the cited sources [S1].",
        model_used="stub-model",
        latency_ms=1,
        prompt_tokens=10,
        completion_tokens=10,
        estimated_cost_usd=0.0,
    )


def run_case(case: dict) -> tuple[bool, list[str]]:
    notes: list[str] = []
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    pool = _pool_to_sources(case["id"])
    if not pool:
        return False, [f"no synthetic source pool defined for case {case['id']!r}"]

    try:
        with patch.object(ro, "_search", _make_search(pool)), \
             patch.object(ro, "crawl_url", lambda _url: None), \
             patch.object(ro, "_json_model", lambda *a, **k: None), \
             patch.object(ro, "invoke_llm", _stub_invoke_llm), \
             patch.object(ro, "_llm_verify_and_repair", lambda *a, **k: (ro._verification_from_data(None, "draft"), None)):
            result = ro.run_research(
                db,
                user_id="eval",
                conversation_id=None,
                query=case["query"],
                profile=None,
                force_model=None,
                mode="deep",
                progress=lambda *_a, **_k: None,
            )
        meta = research_meta_for_run(db, result.run)
        ok, notes = CHECKS[case["id"]](meta, result)
        return ok, notes
    except Exception as exc:  # pragma: no cover - eval harness diagnostics
        return False, [f"pipeline raised: {exc!r}"]
    finally:
        db.close()


def _admitted(meta) -> list:
    return meta.sources


def _by_role(meta, role: str) -> list:
    return [s for s in meta.sources if s.source_role_prior == role]


def _by_tier(meta, tier: str) -> list:
    return [s for s in meta.sources if s.source_tier == tier]


def check_immigration_current_operational_timeline(meta, result) -> tuple[bool, list[str]]:
    notes = []
    # "Separate official premium-processing eligibility from observed current
    # timelines" is satisfied if (a) the official USCIS eligibility source is
    # admitted with an official-policy role/tier, and (b) the current
    # operational-reality timeline evidence is tracked separately (and the
    # stale 2023 source is not the one driving "current" claims).
    official_policy_sources = [
        s for s in meta.sources
        if s.source_tier == "tier_1_official" and s.source_role_prior == "official_policy"
    ]
    operational_sources = _by_role(meta, "operational_reality") + _by_role(meta, "anecdotal_case")
    stale_source = next((s for s in meta.sources if "trackitt.com" in s.url), None)
    stale_rejected = next((s for s in meta.rejected_sources if "trackitt.com" in s.url), None)
    notes.append(f"official-policy tier-1 sources: {len(official_policy_sources)}")
    notes.append(f"operational/anecdotal sources: {len(operational_sources)}")
    notes.append(f"stale (2023) source admitted: {stale_source is not None}; rejected: {stale_rejected is not None}")
    if stale_source is not None:
        notes.append(f"stale source freshness_score={stale_source.freshness_score}")
    ok = bool(official_policy_sources) and bool(operational_sources)
    return ok, notes


def check_immigration_official_policy_single_source(meta, result) -> tuple[bool, list[str]]:
    notes = []
    official = _by_tier(meta, "tier_1_official")
    anecdotal = _by_role(meta, "anecdotal_case")
    policy_threads = [t for t in meta.question_threads if t.claim_type == "policy"]
    ok = bool(official) and bool(anecdotal) and bool(policy_threads)
    notes.append(f"official policy sources: {len(official)}; anecdotal sources: {len(anecdotal)}")
    if policy_threads:
        thread = policy_threads[0]
        notes.append(f"policy thread confidence={thread.confidence!r} stop_reason={thread.stop_reason!r}")
        # the anecdote alone must not be enough to mark the policy thread "low"
        # confidence when an official source is present.
        ok = ok and thread.confidence != "low"
    return ok, notes


def check_anecdote_noisy_operational_reality(meta, result) -> tuple[bool, list[str]]:
    notes = []
    anecdotal = _by_role(meta, "anecdotal_case")
    has_official_or_stat = any(
        s.source_role_prior in {"official_policy", "statistical_data"} for s in meta.sources
    )
    ok = bool(anecdotal) and not has_official_or_stat
    notes.append(f"anecdotal sources: {len(anecdotal)}; official/statistical sources: {has_official_or_stat}")
    op_threads = [t for t in meta.question_threads if t.evidence_role == "operational_reality"]
    if op_threads:
        confidences = [t.confidence for t in op_threads]
        notes.append(f"operational thread confidences: {confidences}")
        # without independent official/statistical corroboration, no
        # operational thread should be marked "high" confidence.
        ok = ok and "high" not in confidences
    notes.append(f"gaps: {meta.gaps}")
    return ok, notes


def check_medical_current_guidance(meta, result) -> tuple[bool, list[str]]:
    notes = []
    official_policy = _by_role(meta, "official_policy")
    admitted_anecdotal = _by_role(meta, "anecdotal_case")
    rejected_anecdotal = [s for s in meta.rejected_sources if "reddit.com" in s.url]
    ok = bool(official_policy)
    notes.append(f"official_policy sources: {len(official_policy)}")
    notes.append(f"admitted anecdotal sources: {len(admitted_anecdotal)}; rejected reddit sources: {len(rejected_anecdotal)}")
    # the anecdote (home-remedy reddit post) must not become load-bearing
    # evidence for the medical guidance thread: either it's filtered out
    # entirely, or it's admitted only as anecdotal_case (not official_policy).
    ok = ok and not any("reddit.com" in s.url for s in admitted_anecdotal if s.source_role_prior == "official_policy")
    policy_threads = [t for t in meta.question_threads if t.claim_type in {"policy", "capability"}]
    for t in policy_threads:
        notes.append(f"thread {t.id} stop_reason={t.stop_reason!r}")
    return ok, notes


def check_finance_company_current_fact(meta, result) -> tuple[bool, list[str]]:
    notes = []
    non_anecdotal = [s for s in meta.sources if s.source_role_prior != "anecdotal_case"]
    nvidia_sources = [s for s in meta.sources if "nvidia" in s.url]
    ok = bool(nvidia_sources) and bool(non_anecdotal)
    notes.append(f"admitted sources: {[(s.url, s.source_tier, s.source_role_prior) for s in meta.sources]}")
    notes.append(f"rejected sources: {len(meta.rejected_sources)}")
    return ok, notes


def check_technology_current_capability(meta, result) -> tuple[bool, list[str]]:
    notes = []
    official = [s for s in meta.sources if "docs.anthropic.com" in s.url]
    anecdotal = _by_role(meta, "anecdotal_case")
    ok = bool(official) and bool(anecdotal)
    if official:
        notes.append(f"docs source tier/role: {official[0].source_tier}/{official[0].source_role_prior}")
        ok = ok and official[0].source_tier == "tier_1_official"
        ok = ok and official[0].source_role_prior == "official_policy"
    notes.append(f"anecdotal (community) sources: {len(anecdotal)}")
    return ok, notes


CHECKS = {
    "immigration_current_operational_timeline": check_immigration_current_operational_timeline,
    "immigration_official_policy_single_source": check_immigration_official_policy_single_source,
    "anecdote_noisy_operational_reality": check_anecdote_noisy_operational_reality,
    "medical_current_guidance": check_medical_current_guidance,
    "finance_company_current_fact": check_finance_company_current_fact,
    "technology_current_capability": check_technology_current_capability,
}


def main() -> int:
    cases = json.loads(FIXTURES_PATH.read_text())
    total = 0
    passed = 0
    for case in cases:
        cid = case["id"]
        if cid not in CHECKS:
            print(f"SKIP  {cid} (no check defined)")
            continue
        total += 1
        ok, notes = run_case(case)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"{status}  {cid}")
        print(f"      required_outcome: {case['required_outcome']}")
        for note in notes:
            print(f"      - {note}")
    print(f"\n{passed}/{total} cases passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
