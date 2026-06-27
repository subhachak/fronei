"""research_synthesis.py — Answer synthesis, ranking, and post-processing.

Responsibilities:
  - synthesize_answer: LLM synthesis over an EvidencePack
  - judge_research: score + pass/fail/repair decision
  - repair_research_answer: targeted answer repair
  - rank_sources / _select_diverse_ranked_sources: source scoring + diversity
  - extract_deep_link_candidates + helpers: deep-link extraction
  - build_gap_followup_workers: gap-closing worker generation
  - _synthesis_report_contract: profile-specific report contracts
  - _synthesis_token_budget: profile + quality mode token limits
  - source_context_from_evidence / _architecture_cards_context: context formatting
  - detect_contradictions re-exported here for backward compat

Extracted from research_subtree.py (TD-01).
"""
from __future__ import annotations

import json
import logging
import re
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from app.services.agent import model_client
from app.services.agent.models import Source, TurnRequest
from app.services.agent.prompt_library import resolve_prompt
from app.services.agent.research_models import (
    DeepLinkCandidate,
    EvidencePack,
    PROFILE_POLICIES,
    RankedSource,
    ResearchJudgeResult,
    ResearchPlan,
    ResearchProfile,
    SearchWorkerPlan,
)
from app.services.agent.research_profiles import (
    REPAIR_PROMPT,
    SYNTHESIS_PROMPT,
)
from app.services.agent.research_planner import _longform_timeout_s
# detect_contradictions lives in research_evidence; re-exported here for backward compat
from app.services.agent.research_evidence import detect_contradictions  # noqa: F401
from app.services.agent.research_utils import (
    _estimate_relevance,
    _extract_urls_from_text,
    classify_source_type,
    score_source_authority,
    score_technical_density,
)

logger = logging.getLogger(__name__)


def is_public_source_url(url: str) -> bool:
    """Return True if the URL is publicly routable (not localhost, private IP, etc.)."""
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(".local"):
        return False
    try:
        ip = ip_address(hostname)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return True

# Phase 2 — claim_role paragraph injected into every synthesis prompt.
# Instructs the model to treat claim_role as the primary epistemic filter and
# never silence operational_reality / anecdotal_case evidence solely because its
# source authority score is lower than official sources.
CLAIM_ROLE_SYNTHESIS_PARAGRAPH = """
--- CLAIM ROLE GUIDANCE ---
Each typed evidence claim carries a claim_role. Use it as the primary epistemic lens:

- official_policy: Describes what a rule or regulation formally requires. Cite as authoritative
  for policy questions. Do NOT treat it as a description of real-world outcomes.
- operational_reality: Describes actual outcomes, wait times, or backlogs in practice.
  This role MUST be used as the primary signal when the question asks "how long does it
  actually take" or "what is really happening". Do not suppress it for having lower authority.
- anecdotal_case: A first-person or individual-case real-world report. When multiple
  independent anecdotal_case claims agree, treat their consensus as operational evidence.
  Do not refuse to answer simply because only anecdotal sources are available.
- expert_interpretation: Analysis or synthesis by a qualified party. Use to frame nuance,
  not to override operational_reality when the question is about real-world outcomes.
- statistical_data: Quantitative measurements. Cite directly; note sample size if known.

When official_policy and operational_reality CONFLICT (e.g. USCIS SLA says 3 months,
practitioners report 8 months), you MUST state BOTH positions explicitly and name the
disagreement. Never silently blend them into a single hedged claim.
--- END CLAIM ROLE GUIDANCE ---
"""


def build_synthesis_prompt(request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack) -> tuple[str, str]:
    architecture_context = _architecture_cards_context(evidence)
    claim_context = "\n".join(
        f"[{claim.source_id}] {claim.claim_type}/{claim.claim_role} "
        f"(confidence={claim.confidence:.2f}, freshness={claim.freshness_risk}): {claim.text}"
        for claim in evidence.claims[:40]
    )
    if not claim_context:
        claim_context = "No typed evidence claims were extracted. Lean on the evidence passages and disclose limits."
    evidence_context = "\n\n".join(
        f"[{item.source_id}] {item.title}\n"
        f"Question: {item.question}\n"
        f"Discovery query: {item.query or 'unknown'}\n"
        f"Provider: {item.provider or 'unknown'}\n"
        f"URL: {item.url}\n"
        f"Evidence: {item.evidence}"
        for item in evidence.items
    )
    if not evidence_context:
        evidence_context = "No source evidence was available. Be transparent about that."
    report_contract = _synthesis_report_contract(plan.research_profile, request)
    # Phase 2 — append claim_role guidance to the resolved system prompt.
    prompt = resolve_prompt(
        "agent.research.synthesis.default",
        agent_id="synthesis",
        fallback_system_prompt=SYNTHESIS_PROMPT,
        variables=["message", "evidence_pack", "profile"],
        profile=plan.research_profile,
    )
    import dataclasses
    prompt = dataclasses.replace(prompt, system_prompt=prompt.system_prompt + CLAIM_ROLE_SYNTHESIS_PARAGRAPH)
    user_prompt = (
        f"{request.conversation_context}\n\n" if request.conversation_context else ""
    ) + (
        f"User request:\n{request.message}\n\n"
        f"Research profile: {plan.research_profile}\n\n"
        f"Secondary profiles: {json.dumps(plan.secondary_profiles, ensure_ascii=False)}\n\n"
        f"Source lanes used: {json.dumps(plan.source_lanes, ensure_ascii=False)}\n\n"
        f"Required deliverable shape:\n{report_contract}\n\n"
        f"Research questions:\n{json.dumps(plan.questions, ensure_ascii=False)}\n\n"
        f"Architecture extraction cards:\n{architecture_context}\n\n"
        f"Typed evidence claims:\n{claim_context}\n\n"
        f"Evidence pack:\n{evidence_context}\n\n"
        f"Known gaps:\n{json.dumps(evidence.gaps, ensure_ascii=False)}"
    )
    return prompt.system_prompt, user_prompt


def synthesize_answer(request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack):
    system_prompt, user_prompt = build_synthesis_prompt(request, plan, evidence)
    return model_client.simple_completion(
        system_prompt,
        user_prompt,
        max_tokens=_synthesis_token_budget(request, plan),
        role="synthesis",
        quality_mode=request.quality_mode,
        overrides=request.model_overrides,
        timeout_s=_longform_timeout_s(),
    )


def judge_research(request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack, answer: str) -> ResearchJudgeResult:
    issues: list[str] = []
    score = 0.45
    if evidence.items:
        score += min(0.25, 0.05 * len(evidence.items))
    if evidence.coverage >= 1.0:
        score += 0.15
    citation_count = len(re.findall(r"\[S\d+\]", answer or ""))
    if citation_count:
        score += min(0.2, 0.05 * citation_count)
    else:
        issues.append("The answer does not include source citations.")
    if len(answer or "") < 180:
        score -= 0.1
        issues.append("The answer is too short for the requested research depth.")
    if evidence.gaps:
        score -= 0.08
        issues.extend(evidence.gaps)
    score = max(0.0, min(1.0, score))
    threshold = plan.judge_threshold or 0.72
    if score >= threshold:
        return ResearchJudgeResult(status="pass", score=score, issues=issues, can_publish=True)
    if score >= max(0.45, threshold - 0.2):
        return ResearchJudgeResult(
            status="repair",
            score=score,
            issues=issues,
            repair_instruction=(
                "Strengthen the answer with explicit [S#] citations, acknowledge source gaps, "
                "and make the structure more useful to the user."
            ),
            can_publish=False,
        )
    return ResearchJudgeResult(
        status="fail",
        score=score,
        issues=issues or ["Research quality is below publish threshold."],
        repair_instruction="Redo the research plan with better source coverage.",
        can_publish=False,
    )


def repair_research_answer(
    request: TurnRequest,
    plan: ResearchPlan,
    evidence: EvidencePack,
    answer: str,
    judge: ResearchJudgeResult,
):
    evidence_context = source_context_from_evidence(evidence) or "No source evidence was available."
    prompt = resolve_prompt(
        "agent.research.repair.default",
        agent_id="repair",
        fallback_system_prompt=REPAIR_PROMPT,
        variables=["answer", "judge", "evidence_pack"],
        profile=plan.research_profile,
    )
    return model_client.simple_completion(
        prompt.system_prompt,
        (
            f"{request.conversation_context}\n\n" if request.conversation_context else ""
        )
        + (
            f"User request:\n{request.message}\n\n"
            f"Original answer:\n{answer}\n\n"
            f"Judge feedback:\n{judge.model_dump_json()}\n\n"
            f"Evidence pack:\n{evidence_context}\n\n"
            f"Research questions:\n{json.dumps(plan.questions, ensure_ascii=False)}"
        ),
        max_tokens=_synthesis_token_budget(request, plan),
        role="repair",
        quality_mode=request.quality_mode,
        overrides=request.model_overrides,
        timeout_s=_longform_timeout_s(),
    )


def rank_sources(sources: list[Source], plan: ResearchPlan) -> list[RankedSource]:
    ranked: list[RankedSource] = []
    seen: set[str] = set()
    for source in sources:
        if not source.url or source.url in seen:
            continue
        seen.add(source.url)
        source_type = classify_source_type(source.url)
        authority = score_source_authority(source.url)
        relevance = _estimate_relevance(source, plan.questions)
        technical_density = score_technical_density(source) if plan.research_profile == "technical_architecture" else 0.0
        content_bonus = 0.08 if source.content else 0.0
        if plan.research_profile == "technical_architecture":
            type_bonus = PROFILE_POLICIES["technical_architecture"].type_weights.get(source_type, 0.0)
            host = urlparse(source.url or "").netloc.lower()
            if "medium.com" in host or "substack.com" in host:
                type_bonus -= 0.08
            score = max(
                0.0,
                min(
                    1.0,
                    (authority * 0.20)
                    + (relevance * 0.24)
                    + (technical_density * 0.38)
                    + type_bonus
                    + content_bonus,
                ),
            )
        else:
            policy = PROFILE_POLICIES.get(plan.research_profile, PROFILE_POLICIES["general"])
            type_bonus = policy.type_weights.get(source_type, 0.0)
            raw = (authority * 0.38) + (relevance * 0.46) + type_bonus + content_bonus
            # Phase 2 floor: prevent low-authority sources (web/news/anecdotal) from
            # being entirely excluded by rank_sources. They still rank lower than
            # official sources but remain eligible for bind_evidence selection.
            score = max(0.05, min(1.0, raw))
        ranked.append(
            RankedSource(
                source=source,
                rank=0,
                score=score,
                source_type=source_type,
                authority=authority,
                relevance=relevance,
                rationale=(
                    f"{source_type} source; authority={authority:.2f}; relevance={relevance:.2f}; "
                    f"technical_density={technical_density:.2f}"
                ),
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    for index, item in enumerate(ranked, start=1):
        item.rank = index
    return ranked


def _select_diverse_ranked_sources(
    ranked: list[RankedSource],
    *,
    limit: int,
    research_level: str,
) -> list[Source]:
    if limit <= 0:
        return []
    host_cap = 5 if research_level == "deep" else 2
    type_targets = {"academic": 8, "repository": 8, "documentation": 8, "primary": 5, "pdf": 6}
    type_minimums = {"academic": 4, "repository": 4, "documentation": 4, "pdf": 2} if research_level == "deep" else {}
    selected: list[RankedSource] = []
    host_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}

    for source_type, minimum in type_minimums.items():
        for item in ranked:
            if len(selected) >= limit or type_counts.get(source_type, 0) >= minimum:
                break
            if item.source_type != source_type:
                continue
            host = urlparse(item.source.url or "").netloc.lower()
            if host_counts.get(host, 0) >= host_cap:
                continue
            selected.append(item)
            host_counts[host] = host_counts.get(host, 0) + 1
            type_counts[source_type] = type_counts.get(source_type, 0) + 1

    for item in ranked:
        if item in selected:
            continue
        host = urlparse(item.source.url or "").netloc.lower()
        source_type = item.source_type
        if host_counts.get(host, 0) >= host_cap:
            continue
        if research_level == "deep" and source_type in type_targets and type_counts.get(source_type, 0) >= type_targets[source_type]:
            continue
        selected.append(item)
        host_counts[host] = host_counts.get(host, 0) + 1
        type_counts[source_type] = type_counts.get(source_type, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_urls = {item.source.url for item in selected}
        for item in ranked:
            if item.source.url in selected_urls:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
    return [item.source for item in selected[:limit]]


def extract_deep_link_candidates(sources: list[Source], *, max_links: int = 4) -> list[DeepLinkCandidate]:
    candidates: list[DeepLinkCandidate] = []
    seen: set[str] = set()
    for source in sources:
        for candidate in _domain_specific_link_candidates(source):
            if candidate.url == source.url or candidate.url in seen or not is_public_source_url(candidate.url):
                continue
            seen.add(candidate.url)
            candidates.append(candidate)
            if len(candidates) >= max_links:
                return candidates
        text = f"{source.snippet}\n{source.content}"
        for url in _extract_urls_from_text(text):
            if url == source.url or url in seen or not is_public_source_url(url):
                continue
            seen.add(url)
            candidates.append(
                DeepLinkCandidate(
                    url=url,
                    parent_url=source.url,
                    reason="Linked from a selected high-value source.",
                )
            )
            if len(candidates) >= max_links:
                return candidates
    return candidates


def _domain_specific_link_candidates(source: Source) -> list[DeepLinkCandidate]:
    url = source.url or ""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    candidates: list[DeepLinkCandidate] = []
    if "arxiv.org" in host:
        arxiv_id = _arxiv_id_from_url(url)
        if arxiv_id:
            candidates.extend(
                [
                    DeepLinkCandidate(url=f"https://arxiv.org/pdf/{arxiv_id}", parent_url=url, reason="arXiv PDF for full paper text."),
                    DeepLinkCandidate(url=f"https://arxiv.org/html/{arxiv_id}", parent_url=url, reason="arXiv HTML paper view when available."),
                ]
            )
    if "github.com" in host:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            repo = f"https://github.com/{parts[0]}/{parts[1]}"
            candidates.extend(
                [
                    DeepLinkCandidate(url=f"{repo}/blob/main/README.md", parent_url=url, reason="Repository README for implementation context."),
                    DeepLinkCandidate(url=f"{repo}/tree/main/docs", parent_url=url, reason="Repository docs folder for architecture details."),
                ]
            )
    return candidates


def _arxiv_id_from_url(url: str) -> str:
    match = re.search(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", url)
    return match.group(1) if match else ""


def _source_inventory_summary(sources: list[Source]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_host: dict[str, int] = {}
    for source in sources:
        source_type = classify_source_type(source.url)
        by_type[source_type] = by_type.get(source_type, 0) + 1
        host = urlparse(source.url or "").netloc.lower() or "unknown"
        by_host[host] = by_host.get(host, 0) + 1
    top_hosts = [
        {"host": host, "count": count}
        for host, count in sorted(by_host.items(), key=lambda item: item[1], reverse=True)[:12]
    ]
    return {
        "total": len([source for source in sources if source.url]),
        "by_type": by_type,
        "top_hosts": top_hosts,
    }


def build_gap_followup_workers(request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack) -> list[SearchWorkerPlan]:
    if not evidence.gaps:
        return []
    gap_text = " ".join(evidence.gaps)[:240]
    query = f"{request.message} {gap_text}".strip()
    return [
        SearchWorkerPlan(
            question=f"Close evidence gap: {gap_text}",
            query=query,
            rationale="Gap agent follow-up search from evidence coverage review.",
            max_results=min(3, plan.max_sources),
        )
    ]

def _synthesis_report_contract(profile: ResearchProfile, request: TurnRequest) -> str:
    if request.output_format == "chat" and "report" not in request.message.lower() and _requires_decision_grade_comparison(request, profile):
        return (
            "Produce a decision-grade research answer in chat, not a terse summary. "
            "Open with an executive recommendation that names the winner, the decision rule, and the main decision constraint. "
            "Do not open with an evidence-quality disclaimer; if the evidence is too weak for a decision-grade answer, "
            "the research judge should request more research rather than publishing a disclaimer-heavy answer. "
            "Then include a compact comparison matrix using the user's requested dimensions. "
            "For each named option, provide: architecture model, coordination approach, production readiness, known failure modes, "
            "and best-fit / avoid-when guidance. Use concrete mechanisms and named product/lifecycle signals from evidence; "
            "do not substitute generic pros and cons. "
            "Include a cross-cutting failure taxonomy or governance lens when the question asks about production, enterprise, "
            "or orchestration. Explicitly flag lifecycle, maintenance, successor-framework, or ecosystem shifts when evidence shows them. "
            "Close with a ranked recommendation and conditional overrides, e.g. default choice, cloud/vendor-lock override, "
            "RAG/search override, prototyping override. "
            "Cite factual claims with [S#]. If narrow benchmark, adoption, failure-rate, or production-use details remain missing, "
            "capture them as short validation notes near the relevant row or recommendation, not as a dominant disclaimer. "
            "For any named framework where a specific dimension (e.g. failure modes, production readiness) lacks evidence, "
            "write the best-effort content from what IS available and add a single inline note such as "
            "'*(no public failure-mode evidence found in retrieved sources)*' — do not substitute an entire section with "
            "a validation note, and never produce a meta-commentary block titled 'Honest status' or 'Evidence quality disclaimer'. "
            "A best-effort answer with disclosed gaps is always preferable to a refusal to publish."
        )
    if request.output_format == "chat" and "report" not in request.message.lower() and _requests_brief_answer(request):
        return (
            "Produce a concise chat answer, not a report or artifact. "
            "Follow the user's requested shape exactly. Prefer a short ranked list or compact bullets over large tables. "
            "For comparisons, include only the fields the user asked for, cite factual claims with [S#], "
            "name the most promising option, and keep caveats brief."
        )
    if request.output_format == "chat" and "report" not in request.message.lower():
        return (
            "Produce an elaborative, source-grounded chat answer by default, not a terse summary. "
            "Use clear headings and enough detail that the answer can stand alone without the user needing to ask for a deeper version. "
            "Follow the user's requested shape exactly, but expand each requested dimension with concrete findings, trade-offs, "
            "failure modes, caveats, and implications where the evidence supports them. "
            "For comparisons, use a readable matrix or consistent per-option sections, then synthesize the practical takeaway. "
            "For recommendation questions, state the decision rule, the top recommendation, why it wins, where it does not fit, "
            "and what validation the user should run next. "
            "Cite factual claims with [S#]. If evidence is missing or uneven, disclose that instead of smoothing over the gap. "
            "Only be brief when the user explicitly asks for brevity."
        )
    if profile == "technical_architecture":
        return (
            "Produce a detailed architectural report. "
            "Derive the section structure from the evidence — use the components, workflows, "
            "and architectural patterns that actually appear in the sources, not a generic template. "
            "Use the architecture extraction cards as the primary spine: compare named systems, their state objects, "
            "agent roles, renderers/tools, validation loops, metrics, and failure modes. "
            "Every section must be grounded in specific evidence with [S#] citations. "
            "Include concrete implementation details: data models, control flow, state transitions, "
            "failure handling, trade-offs, and design decisions. "
            "Add a compact ASCII or text diagram where it clarifies a component relationship or data flow. "
            "Where sources conflict or leave gaps, say so explicitly rather than filling with generic description. "
            "Avoid restating definitions unless the definition itself contains a design decision worth citing. "
            "For deep research, target 10-14 substantive sections and enough detail to stand alone as a technical "
            "architecture brief: concrete mechanisms, named systems, implementation patterns, trade-offs, failure "
            "modes, and source-backed examples. Do not compress the report into a short summary."
        )
    if profile == "vendor_comparison":
        return (
            "Produce a structured vendor comparison report. "
            "Open with a 1-paragraph executive summary naming the top recommendation and the key differentiator. "
            "Then evaluate each vendor or option against: pricing and licensing, API capabilities, security and compliance, "
            "SLAs and reliability, use-case fit, vendor risk, and switching costs. "
            "Use a consistent evaluation framework across all vendors so the reader can compare directly. "
            "Every claim must be [S#] cited — do not fill in gaps with generic vendor marketing language. "
            "Close with a scored or ranked recommendation matrix and a clear rationale for the top pick. "
            "Where data is missing or unverifiable, flag it explicitly as a gap rather than omitting it."
        )
    if profile == "market_landscape":
        return (
            "Produce a market landscape analysis. "
            "Open with a 1-paragraph market framing: what the space is, why it matters now, and the key dynamic. "
            "Then cover: market segmentation and categories, key players with positioning, "
            "quantitative metrics (market size, growth rate, adoption), technology trends, "
            "business model patterns, buyer behavior, and competitive dynamics. "
            "Ground every claim in [S#] cited evidence — prefer analyst reports, earnings calls, and primary sources. "
            "Where metrics conflict across sources, present both and note the discrepancy. "
            "Close with business implications: what the trends mean for a buyer, investor, or competitive entrant."
        )
    if profile == "policy_regulatory":
        return (
            "Produce a regulatory analysis. "
            "Lead with a plain-language summary of the primary obligation and who it applies to. "
            "For each regulation: name the authoritative source, the enforcement body, the jurisdiction, "
            "the effective date, and the specific compliance requirements. "
            "Cover penalties and enforcement history with [S#] citations to enforcement actions. "
            "Distinguish between binding requirements and guidance/safe-harbor interpretations. "
            "Flag pending regulatory changes or open consultations that could shift requirements. "
            "Do not conflate jurisdictions — keep requirements for each jurisdiction clearly separated. "
            "Close with a compliance action checklist: what an organization must do, by when."
        )
    if profile == "strategy_brief":
        return (
            "Produce an executive strategy brief. "
            "Open with a 1-paragraph executive summary: the decision, the recommendation, and the key rationale. "
            "Then: frame the business context and problem with evidence; present 2-4 strategic options "
            "with trade-offs; state the recommended option with clear rationale and risk acknowledgment; "
            "identify the top 3-5 risks with mitigations; quantify resource, cost, and timeline implications; "
            "define success metrics. Close with a concrete next-steps section: owner, action, deadline. "
            "Use crisp, decision-grade language — avoid hedging. "
            "Every factual claim must be [S#] cited."
        )
    if profile == "implementation_plan":
        return (
            "Produce a structured implementation plan. "
            "Open with scope, objectives, and measurable success criteria. "
            "Then: break work into named workstreams with tasks and inter-dependencies; "
            "define milestones with dates or relative timelines; assign owner roles to each workstream; "
            "present a risk register with likelihood, impact, and mitigation for each risk; "
            "describe governance, communication cadence, and change management approach; "
            "include rollback and contingency scenarios. "
            "Use tables or structured lists for the workstream breakdown, milestone timeline, and risk register. "
            "Ground planning assumptions in [S#] cited evidence where relevant. "
            "Close with a go/no-go decision checklist for the first milestone gate."
        )
    if request.output_format in {"docx", "markdown", "pptx"} or "report" in request.message.lower():
        return "Produce a structured report with clear headings, evidence-backed findings, gaps, and recommendations."
    return "Produce a source-grounded answer with clear headings and cited findings."


def _synthesis_token_budget(request: TurnRequest, plan: ResearchPlan) -> int:
    profile = plan.research_profile
    is_deep = request.research_level == "deep"
    is_exec = request.quality_mode == "executive"
    if request.output_format == "chat" and _requests_brief_answer(request):
        return 1800 if is_exec else 1200
    if request.output_format == "chat" and _requires_decision_grade_comparison(request, profile):
        return (11000 if is_deep else 9500) if is_exec else (9500 if is_deep else 8000)
    if profile == "technical_architecture" and is_deep:
        # Deep technical report: needs room for 10 detailed sections with citations,
        # diagrams, trade-off tables, and implementation specifics.
        return 14000 if is_exec else 12000
    if profile == "vendor_comparison":
        # Comparison table + per-vendor sections + recommendation matrix
        return (10000 if is_deep else 7000) if is_exec else (8000 if is_deep else 5500)
    if profile == "market_landscape":
        # Market overview + player profiles + trends + business implications
        return (9000 if is_deep else 6500) if is_exec else (7500 if is_deep else 5000)
    if profile == "policy_regulatory":
        # Per-regulation breakdown + jurisdiction table + compliance checklist
        return (9000 if is_deep else 6000) if is_exec else (7500 if is_deep else 5000)
    if profile == "strategy_brief":
        # Executive brief — dense but not sprawling; options analysis + rec + next steps
        return (7000 if is_deep else 4500) if is_exec else (6000 if is_deep else 3500)
    if profile == "implementation_plan":
        # Workstream breakdown, milestones, risk register, governance
        return (9000 if is_deep else 6000) if is_exec else (7500 if is_deep else 5000)
    if request.output_format in {"docx", "markdown", "pptx"} or "report" in request.message.lower():
        return 6500 if is_exec else 5200
    if request.output_format == "chat":
        return (6200 if is_deep else 5200) if is_exec else (5000 if is_deep else 4200)
    return 1800 if is_exec else 1200


def _requests_brief_answer(request: TurnRequest) -> bool:
    text = f" {request.message or ''} ".lower()
    brief_patterns = (
        r"\bbriefly\b",
        r"\bconcise(?:ly)?\b",
        r"\bquick(?:ly)?\b",
        r"\bshort answer\b",
        r"\bshort version\b",
        r"\bquick summary\b",
        r"\bsummar(?:y|ize) briefly\b",
        r"\btl;?dr\b",
        r"\btldr\b",
        r"\bkeep it short\b",
        r"\bkeep this short\b",
        r"\bbe brief\b",
        r"\bin brief\b",
        r"\bin \d+ (?:sentence|sentences|bullet|bullets|paragraph|paragraphs)\b",
        r"\b(?:one|two|three|four|five) (?:sentence|sentences|bullet|bullets|paragraph|paragraphs)\b",
        r"\bno more than \d+ (?:words|sentence|sentences|bullet|bullets|paragraph|paragraphs)\b",
        r"\bunder \d+ words\b",
    )
    return any(re.search(pattern, text) for pattern in brief_patterns)


def _requires_decision_grade_comparison(request: TurnRequest, profile: ResearchProfile) -> bool:
    text = (request.message or "").lower()
    if profile not in {"vendor_comparison", "technical_architecture", "strategy_brief"}:
        return False
    comparison_signal = bool(
        re.search(r"\btop\s+\d+\b", text)
        or any(term in text for term in (
            "compare",
            "comparison",
            "versus",
            " vs ",
            "evaluate",
            "evaluation",
            "matrix",
            "for each",
            "best framework",
            "best platform",
            "best tool",
            "recommendation",
            "recommend ",
        ))
    )
    decision_signal = any(term in text for term in (
        "enterprise",
        "production",
        "orchestration",
        "orchestration layer",
        "production readiness",
        "failure modes",
        "known failure",
        "coordination",
        "multi-agent",
        "multi agent",
        "architecture model",
    ))
    named_options = _count_named_options(request.message or "")
    return comparison_signal and decision_signal and named_options >= 3


def _count_named_options(message: str) -> int:
    # Count comma/semicolon/colon-delimited title-ish candidates in the request.
    # This is intentionally heuristic; it decides answer depth, not facts.
    candidate_region = message
    if ":" in message:
        candidate_region = message.split(":", 1)[1]
    stop_match = re.search(r"\bprovide for each\b|\bthen synthesize\b|\bexplain why\b", candidate_region, flags=re.IGNORECASE)
    if stop_match:
        candidate_region = candidate_region[:stop_match.start()]
    candidates = re.split(r",|;|\band\b", candidate_region)
    count = 0
    for raw in candidates:
        value = raw.strip(" .:-()[]")
        if not value:
            continue
        if re.search(r"\b[A-Z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*)?\b", value):
            count += 1
    return count


def source_context_from_evidence(evidence: EvidencePack) -> str:
    architecture_context = _architecture_cards_context(evidence)
    claim_context = "\n".join(
        f"[{claim.source_id}] {claim.claim_type}/{claim.claim_role}: {claim.text}"
        for claim in evidence.claims[:30]
    )
    passage_context = "\n\n".join(
        f"[{item.source_id}] {item.title}\nURL: {item.url}\n{item.evidence}"
        for item in evidence.items
    )
    if claim_context and passage_context:
        return f"Architecture extraction cards:\n{architecture_context}\n\nTyped evidence claims:\n{claim_context}\n\nEvidence passages:\n{passage_context}"
    if architecture_context:
        return f"Architecture extraction cards:\n{architecture_context}\n\nEvidence passages:\n{passage_context}"
    return passage_context


def _architecture_cards_context(evidence: EvidencePack) -> str:
    if not evidence.architecture_cards:
        return "No architecture extraction cards were built. Use typed claims and passages, but disclose missing implementation detail."
    lines: list[str] = []
    for card in evidence.architecture_cards[:18]:
        lines.append(
            "\n".join(
                [
                    f"- [{card.source_id}] {card.system}",
                    f"  Pattern: {card.architecture_pattern or 'unknown'}",
                    f"  Agent roles: {', '.join(card.agent_roles) or 'unknown'}",
                    f"  State objects: {', '.join(card.state_objects) or 'unknown'}",
                    f"  Tools/renderers: {', '.join(card.tools_or_renderers) or 'unknown'}",
                    f"  Validation loop: {card.validation_loop or 'unknown'}",
                    f"  Failure modes: {', '.join(card.failure_modes) or 'unknown'}",
                    f"  Metrics: {' | '.join(card.metrics) or 'unknown'}",
                    f"  Lesson for AgentDeck: {card.lesson_for_agentdeck}",
                    f"  Quote: {card.quote or 'none extracted'}",
                    f"  URL: {card.source_url}",
                ]
            )
        )
    return "\n".join(lines)




__all__ = [
    "build_gap_followup_workers",
    "extract_deep_link_candidates",
    "is_public_source_url",
    "judge_research",
    "rank_sources",
    "repair_research_answer",
    "source_context_from_evidence",
    "synthesize_answer",
    "_architecture_cards_context",
    "_arxiv_id_from_url",
    "_domain_specific_link_candidates",
    "_select_diverse_ranked_sources",
    "_source_inventory_summary",
    "_synthesis_report_contract",
    "_synthesis_token_budget",
    "detect_contradictions",
]
