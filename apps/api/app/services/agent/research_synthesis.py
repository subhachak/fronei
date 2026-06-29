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
    owner_reliability_issues = _owner_reliability_answer_issues(request, answer)
    if owner_reliability_issues:
        score -= min(0.35, 0.16 * len(owner_reliability_issues))
        issues.extend(owner_reliability_issues)
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


def _owner_reliability_answer_issues(request: TurnRequest, answer: str) -> list[str]:
    message = (request.message or "").lower()
    text = (answer or "").lower()
    if not _is_owner_reliability_request(message):
        return []
    issues: list[str] = []
    if re.search(r"\b(?:no|not|none)\b.{0,80}\b(?:owner|reddit|forum|community|longitudinal|1[–-]2 year|12[–-]24 month)", text):
        issues.append("Answer admits the requested owner/community longitudinal evidence was not retrieved.")
    if re.search(r"\b(?:cannot|can't)\s+(?:give|provide|characterize)\b.{0,120}\b(?:reliability|failure-rate|failure rate|verdict|consensus)", text):
        issues.append("Answer cannot deliver the requested reliability verdict from the retrieved evidence.")
    if "policy only" in text and any(term in text for term in ("warranty", "official_policy", "official policy")):
        issues.append("Answer relies on warranty/policy evidence for an owner reliability question.")
    return issues


def _is_owner_reliability_request(message: str) -> bool:
    owner_terms = (
        "owner review",
        "owner reviews",
        "owner report",
        "owner reports",
        "owner experience",
        "owner experiences",
        "owners say",
        "user reviews",
        "customer reviews",
        "reddit",
        "forum",
        "community",
        "real-world",
        "real world",
    )
    reliability_terms = (
        "reliability",
        "failure rate",
        "failure rates",
        "failures",
        "degradation",
        "capacity retention",
        "long-term",
        "long term",
        "after 1",
        "after 2",
        "1-2 years",
        "1–2 years",
        "warranty claim",
    )
    return any(term in message for term in owner_terms) and any(term in message for term in reliability_terms)


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
            if (
                url == source.url
                or url in seen
                or not is_public_source_url(url)
                or not _is_useful_deep_link(url)
            ):
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


def _is_useful_deep_link(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower().strip("/")
    if not path:
        return False
    fragment = (parsed.fragment or "").lower()
    if fragment in {"main-content", "content", "top"}:
        return False
    blocked_hosts = {
        "connect.facebook.net",
        "facebook.com",
        "www.facebook.com",
        "google-analytics.com",
        "www.google-analytics.com",
        "googletagmanager.com",
        "www.googletagmanager.com",
        "avatars.githubusercontent.com",
        "docs.github.com",
        "www.w3.org",
    }
    if host in blocked_hosts or host.endswith(".facebook.com"):
        return False
    raw_path = f"/{path}"
    query = (parsed.query or "").lower()
    if raw_path in {"/tr", "/collect", "/pixel"} or "pageview" in query:
        return False
    if re.search(r"\.(?:png|jpe?g|gif|webp|svg|ico|css|js|woff2?|ttf|mp4|mov|zip)(?:$|\?)", path):
        return False
    if "svg" in path and host.endswith("w3.org"):
        return False
    blocked_segments = {
        "contact",
        "contact-us",
        "demo",
        "demo-2",
        "pricing",
        "login",
        "signin",
        "sign-in",
        "privacy-policy",
        "cookie-policy",
        "category",
        "tag",
        "topic",
        "topics",
        "series",
        "column",
        "author",
        "authors",
    }
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in blocked_segments for segment in segments):
        return False
    if host.endswith("ncbi.nlm.nih.gov") and any(segment in {"myncbi", "account", "login", "settings"} for segment in segments):
        return False
    if "logo" in path or "wp-content/uploads" in path:
        return False
    return True


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
    owner_workers = _owner_reliability_gap_followup_workers(request, plan, evidence)
    if owner_workers:
        return owner_workers
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


def _owner_reliability_gap_followup_workers(
    request: TurnRequest, plan: ResearchPlan, evidence: EvidencePack
) -> list[SearchWorkerPlan]:
    message = request.message or ""
    if not _is_owner_reliability_request(message):
        return []
    gap_text = " ".join(evidence.gaps).lower()
    if not any(term in gap_text for term in ("owner", "community", "forum", "failure rate", "degradation", "claim")):
        return []

    lower = message.lower()
    if "anker" in lower and "solix" in lower:
        queries = [
            "Anker SOLIX F3800 owner review reliability failure degradation 12 months 18 months",
            "Anker SOLIX X1 owner review reliability failure warranty degradation 12 months 18 months",
            "site:reddit.com Anker SOLIX F3800 X1 reliability failure warranty degradation",
            "site:diysolarforum.com Anker SOLIX F3800 X1 owner reliability degradation warranty",
        ]
    else:
        subject = re.sub(r"\s+", " ", message).strip()[:120]
        queries = [
            f"{subject} owner review reliability failure degradation 12 months",
            f"{subject} forum reddit reliability failure warranty degradation",
            f"site:reddit.com {subject} reliability failure warranty degradation",
        ]

    max_results = min(6, max(3, plan.max_sources // 2 if plan.max_sources else 3))
    return [
        SearchWorkerPlan(
            question=f"Close owner reliability evidence gap: {query}",
            query=query[:220],
            rationale="Gap agent follow-up search targeting dated owner, forum, degradation, and warranty evidence.",
            max_results=max_results,
        )
        for query in queries
    ]

# Phase 7 — Behavioral guardrails extracted as a single constant, appended to every
# branch of _synthesis_report_contract so ALL queries receive them — not just those
# that trip _requires_decision_grade_comparison().
#
# Previously these three instructions existed only inside the decision-grade-comparison
# branch, leaving single-subject operational-reality queries (e.g. H-4 EAD) with no
# best-effort/no-disclaimer/lifecycle-flagging guidance at all.
SYNTHESIS_SUBSTANCE_REQUIREMENTS = """
--- BEHAVIORAL REQUIREMENTS (apply to every answer regardless of profile or format) ---
1. CITATIONS: Cite every factual claim with [S#] using the source IDs in the evidence pack. Do not make claims without citations.
2. LIFECYCLE FLAGS: Explicitly flag lifecycle, maintenance-mode, successor-framework, or ecosystem/status shifts (deprecation, rename, end-of-life, successor GA) whenever evidence shows them. Place this in the main body next to the relevant subject — not a footnote, not a validation note block at the end.
3. NO LEADING DISCLAIMER: Do not open with an evidence-quality disclaimer or caveat block. If evidence is thin for a specific subject or dimension, disclose that gap inline — next to the relevant claim, section header, or named entity — not as a dominant opening block titled "Evidence quality" or similar.
4. BEST EFFORT OVER REFUSAL: A best-effort answer with inline gap disclosures is always preferable to withholding the answer, refusing to publish, or leading with "I cannot provide a complete answer because...". Publish what the evidence supports and flag what it doesn't.
5. NEVER ASK PERMISSION TO DO MORE RESEARCH: Do not end the answer by asking the user whether to authorize, approve, or proceed with further research, a second pass, or a deeper dive. If more research would improve the answer and budget allows, that research should already have happened before this answer was written — not be offered as a follow-up question. If budget is genuinely exhausted, state the specific remaining gap plainly as part of the answer and move on; do not solicit permission to continue. Prohibited endings: "Let me know if you'd like me to research X further", "Would you like a deeper dive into...", "I can do a second pass on... if you'd like", "Should I look into... in more detail?", or any variant.
6. NO TAXONOMY TOKEN LEAKAGE: Internal claim taxonomy labels — claim_role values (e.g. "primary_claim", "supporting_evidence", "contextual_background", "counter_claim") and claim_type values (e.g. "statistical_data", "operational_reality", "expert_opinion", "vendor_claim") — exist solely to guide your internal reasoning about hedging, confidence framing, and structural emphasis. They must never appear as literal token names in the rendered answer. Never write phrases like "This is a statistical_data claim", "as primary_claim evidence shows", or backtick-wrap taxonomy labels in prose. Express the underlying meaning in natural language instead (e.g., "Studies show...", "In practice...", "According to the vendor...").
--- END BEHAVIORAL REQUIREMENTS ---
"""


def _synthesis_report_contract(profile: ResearchProfile, request: TurnRequest) -> str:
    # Phase 7 — SYNTHESIS_SUBSTANCE_REQUIREMENTS is appended to every branch below.
    # Each branch's return is constructed as: structural_guidance + SYNTHESIS_SUBSTANCE_REQUIREMENTS
    S = SYNTHESIS_SUBSTANCE_REQUIREMENTS  # local alias for brevity

    if request.output_format == "chat" and "report" not in request.message.lower() and _requires_decision_grade_comparison(request, profile):
        # Phase 7 — replaced rigid structural mandate (matrix → per-option fields → ranked rec)
        # with a flexible instruction that lets evidence density determine structure.
        # _requires_decision_grade_comparison() still gates depth ("decision-grade") but no
        # longer prescribes exact section shape, preventing placeholder cells for thin-evidence subjects.
        return (
            "Produce a decision-grade research answer in chat, not a terse summary. "
            "Choose whatever structure best serves this request given the evidence actually retrieved — "
            "matrix, prose, decision tree, grouped by theme, or something else. "
            "Let evidence density per subject guide section depth; don't force identical sub-sections onto "
            "every named option if the evidence doesn't support it for all of them. "
            "Include an executive recommendation that names the recommended option, the decision rule, and "
            "the main decision constraint. "
            "Use concrete mechanisms and named product/lifecycle signals from evidence; "
            "do not substitute generic pros and cons. "
            "Include a cross-cutting failure taxonomy or governance lens when the question asks about "
            "production, enterprise, or orchestration. "
            "Close with a ranked recommendation and conditional overrides when the evidence supports them "
            "(e.g. default choice, cloud/vendor-lock override, RAG/search override, prototyping override). "
            "If narrow benchmark, adoption, failure-rate, or production-use details are missing for a specific "
            "subject, write best-effort content from what IS available and add a single inline note such as "
            "'*(no public failure-mode evidence retrieved)*' — not an entire section of meta-commentary."
        ) + S
    if request.output_format == "chat" and "report" not in request.message.lower() and _requests_brief_answer(request):
        return (
            "Produce a concise chat answer, not a report or artifact. "
            "Follow the user's requested shape exactly. Prefer a short ranked list or compact bullets over large tables. "
            "For comparisons, include only the fields the user asked for, "
            "name the most promising option, and keep caveats brief."
        ) + S
    if request.output_format == "chat" and "report" not in request.message.lower():
        return (
            "Produce an elaborative, source-grounded chat answer by default, not a terse summary. "
            "Use clear headings and enough detail that the answer can stand alone without the user needing to ask for a deeper version. "
            "Follow the user's requested shape exactly, but expand each requested dimension with concrete findings, trade-offs, "
            "failure modes, caveats, and implications where the evidence supports them. "
            "For comparisons, use a readable matrix or consistent per-option sections, then synthesize the practical takeaway. "
            "For recommendation questions, state the decision rule, the top recommendation, why it wins, where it does not fit, "
            "and what validation the user should run next. "
            "If evidence is missing or uneven for a specific dimension or subject, disclose that inline rather than smoothing over the gap. "
            "Only be brief when the user explicitly asks for brevity."
        ) + S
    if profile == "technical_architecture":
        return (
            "Produce a detailed architectural report. "
            "Derive the section structure from the evidence — use the components, workflows, "
            "and architectural patterns that actually appear in the sources, not a generic template. "
            "Use the architecture extraction cards as the primary spine: compare named systems, their state objects, "
            "agent roles, renderers/tools, validation loops, metrics, and failure modes. "
            "Every section must be grounded in specific evidence. "
            "Include concrete implementation details: data models, control flow, state transitions, "
            "failure handling, trade-offs, and design decisions. "
            "Add a compact ASCII or text diagram where it clarifies a component relationship or data flow. "
            "Where sources conflict or leave gaps, say so explicitly rather than filling with generic description. "
            "Avoid restating definitions unless the definition itself contains a design decision worth citing. "
            "For deep research, target 10-14 substantive sections and enough detail to stand alone as a technical "
            "architecture brief: concrete mechanisms, named systems, implementation patterns, trade-offs, failure "
            "modes, and source-backed examples. Do not compress the report into a short summary."
        ) + S
    if profile == "vendor_comparison":
        return (
            "Produce a structured vendor comparison report. "
            "Open with a 1-paragraph executive summary naming the top recommendation and the key differentiator. "
            "Then evaluate each vendor or option against: pricing and licensing, API capabilities, security and compliance, "
            "SLAs and reliability, use-case fit, vendor risk, and switching costs. "
            "Use a consistent evaluation framework across all vendors so the reader can compare directly. "
            "Do not fill in gaps with generic vendor marketing language. "
            "Close with a scored or ranked recommendation matrix and a clear rationale for the top pick. "
            "Where data is missing or unverifiable, flag it explicitly as an inline gap rather than omitting it."
        ) + S
    if profile == "market_landscape":
        return (
            "Produce a market landscape analysis. "
            "Open with a 1-paragraph market framing: what the space is, why it matters now, and the key dynamic. "
            "Then cover: market segmentation and categories, key players with positioning, "
            "quantitative metrics (market size, growth rate, adoption), technology trends, "
            "business model patterns, buyer behavior, and competitive dynamics. "
            "Ground every claim in cited evidence — prefer analyst reports, earnings calls, and primary sources. "
            "Where metrics conflict across sources, present both and note the discrepancy. "
            "Close with business implications: what the trends mean for a buyer, investor, or competitive entrant."
        ) + S
    if profile == "policy_regulatory":
        return (
            "Produce a regulatory analysis. "
            "Lead with a plain-language summary of the primary obligation and who it applies to. "
            "For each regulation: name the authoritative source, the enforcement body, the jurisdiction, "
            "the effective date, and the specific compliance requirements. "
            "Cover penalties and enforcement history with cited enforcement actions. "
            "Distinguish between binding requirements and guidance/safe-harbor interpretations. "
            "Flag pending regulatory changes or open consultations that could shift requirements. "
            "Do not conflate jurisdictions — keep requirements for each jurisdiction clearly separated. "
            "Close with a compliance action checklist: what an organization must do, by when."
        ) + S
    if profile == "strategy_brief":
        return (
            "Produce an executive strategy brief. "
            "Open with a 1-paragraph executive summary: the decision, the recommendation, and the key rationale. "
            "Then: frame the business context and problem with evidence; present 2-4 strategic options "
            "with trade-offs; state the recommended option with clear rationale and risk acknowledgment; "
            "identify the top 3-5 risks with mitigations; quantify resource, cost, and timeline implications; "
            "define success metrics. Close with a concrete next-steps section: owner, action, deadline. "
            "Use crisp, decision-grade language — avoid hedging."
        ) + S
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
            "Ground planning assumptions in cited evidence where relevant. "
            "Close with a go/no-go decision checklist for the first milestone gate."
        ) + S
    if request.output_format in {"docx", "markdown", "pptx"} or "report" in request.message.lower():
        return "Produce a structured report with clear headings, evidence-backed findings, gaps, and recommendations." + S
    return "Produce a source-grounded answer with clear headings and cited findings." + S


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
    "SYNTHESIS_SUBSTANCE_REQUIREMENTS",
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
