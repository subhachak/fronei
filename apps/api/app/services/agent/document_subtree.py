from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.agent import model_client
from app.services.agent.models import TurnRequest, Artifact, Source, ToolCall
from app.services.agent.prompt_library import resolve_prompt
from app.services.agent.research_subtree import EvidencePack, infer_research_profile, source_context_from_evidence
from app.services.agent.tools import source_context

logger = logging.getLogger(__name__)


class DocumentPlan(BaseModel):
    title: str = "Fronei document"
    format: Literal["markdown", "docx", "pptx"] = "docx"
    audience: str = "general business audience"
    sections: list[str] = Field(default_factory=list)
    source: str = "llm"
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    fallback_reason: str | None = None


class DocumentDraft(BaseModel):
    markdown: str
    model_used: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    model_role: str = "document_writer"
    preferred_model: str = ""
    attempted_models: list[str] = Field(default_factory=list)
    failed_model_attempts: list[dict[str, str]] = Field(default_factory=list)


class DocumentJudgeResult(BaseModel):
    status: Literal["pass", "repair"] = "pass"
    score: float = 1.0
    issues: list[str] = Field(default_factory=list)
    repair_instruction: str = ""


@dataclass
class SectionWriteResult:
    index: int
    markdown: str
    model_used: str
    latency_ms: int
    cost_usd: float
    attempted_models: list[str]
    failed_model_attempts: list[dict[str, str]]


PLAN_PROMPT = """You are the Fronei document planner.

Create a document plan sized to the user request. Return only JSON:
{
  "title": "short document title",
  "format": "markdown|docx|pptx",
  "audience": "intended audience",
  "sections": ["concrete section headings"]
}
For ordinary documents, use 4-7 sections. For deep technical reports, use 10-14
substantive sections that preserve the important architecture, implementation,
evidence, trade-off, failure-mode, and recommendation areas from the research.
Prefer docx when the user asks for a downloadable report/document.
Prefer pptx when the user asks for slides, a deck, presentation, or PowerPoint.
"""


def plan_document(
    request: TurnRequest,
    *,
    sources: list[Source],
    research_answer: str | None = None,
    evidence: EvidencePack | None = None,
) -> DocumentPlan:
    try:
        prompt = resolve_prompt(
            "agent.document.plan.default",
            agent_id="document_planner",
            fallback_system_prompt=PLAN_PROMPT,
            variables=["message", "research_answer", "output_format"],
            profile=infer_research_profile(request.message),
        )
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": request.message,
                            "conversation_context": request.conversation_context[-5000:] if request.conversation_context else "",
                            "quality_mode": request.quality_mode,
                            "output_format": request.output_format,
                            "research_summary": _planner_research_summary(request, research_answer),
                            "source_count": len(sources),
                            "evidence_count": len(evidence.items) if evidence else 0,
                            "section_guidance": _section_guidance(request, research_answer=research_answer),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            role="document_planner",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=600,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        plan = DocumentPlan.model_validate(payload)
        plan.model_used = response.model_used
        plan.latency_ms = response.latency_ms
        plan.cost_usd = response.cost_usd
        plan.source = "llm"
        return _normalize_plan(plan, request)
    except Exception as exc:
        logger.warning("agent document planning failed; using fallback plan: %s", exc)
        plan = _fallback_plan(request)
        plan.fallback_reason = str(exc)
        return plan


def write_document(
    request: TurnRequest,
    plan: DocumentPlan,
    *,
    sources: list[Source],
    research_answer: str | None = None,
    evidence: EvidencePack | None = None,
    repair_instruction: str | None = None,
) -> DocumentDraft:
    if _should_write_by_section(request, plan, research_answer=research_answer):
        return _write_document_by_section(
            request,
            plan,
            sources=sources,
            research_answer=research_answer,
            evidence=evidence,
            repair_instruction=repair_instruction,
        )
    return _write_document_single_call(
        request,
        plan,
        sources=sources,
        research_answer=research_answer,
        evidence=evidence,
        repair_instruction=repair_instruction,
    )


def _write_document_single_call(
    request: TurnRequest,
    plan: DocumentPlan,
    *,
    sources: list[Source],
    research_answer: str | None = None,
    evidence: EvidencePack | None = None,
    repair_instruction: str | None = None,
) -> DocumentDraft:
    context = source_context_from_evidence(evidence) if evidence is not None else source_context(sources)
    prompt = (
        (f"{request.conversation_context}\n\n" if request.conversation_context else "")
        +
        f"User request:\n{request.message}\n\n"
        f"Document plan:\n{plan.model_dump_json()}\n\n"
        f"Research summary:\n{research_answer or ''}\n\n"
        f"Sources:\n{context}\n\n"
    )
    if repair_instruction:
        prompt += f"Repair instruction:\n{repair_instruction}\n\n"
    prompt += _document_writer_instruction(request, research_answer=research_answer)
    system_prompt = resolve_prompt(
        "agent.document.write.default",
        agent_id="document_writer",
        fallback_system_prompt="You are the Fronei document writer. Produce only the document body in markdown.",
        variables=["message", "plan", "research_answer"],
        profile=infer_research_profile(request.message),
    )
    response = model_client.simple_completion(
        system_prompt.system_prompt,
        prompt,
        max_tokens=_document_writer_token_budget(request, research_answer=research_answer),
        role="document_writer",
        quality_mode=request.quality_mode,
        overrides=request.model_overrides,
        timeout_s=max(30, int(get_settings().longform_timeout_s or 180)),
    )
    return DocumentDraft(
        markdown=response.text,
        model_used=response.model_used,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
        model_role=getattr(response, "model_role", "document_writer"),
        preferred_model=getattr(response, "preferred_model", ""),
        attempted_models=list(getattr(response, "attempted_models", []) or []),
        failed_model_attempts=list(getattr(response, "failed_model_attempts", []) or []),
    )


def _write_document_by_section(
    request: TurnRequest,
    plan: DocumentPlan,
    *,
    sources: list[Source],
    research_answer: str | None = None,
    evidence: EvidencePack | None = None,
    repair_instruction: str | None = None,
) -> DocumentDraft:
    sections = plan.sections or ["Executive summary", "Findings", "Recommendations"]
    written_sections: list[str] = []
    model_used = ""
    latency_ms = 0
    cost_usd = 0.0
    attempted_models: list[str] = []
    failed_model_attempts: list[dict[str, str]] = []
    timeout_s = max(30, int(get_settings().longform_timeout_s or 180))
    max_workers = _document_writer_concurrency(len(sections))
    outline = "\n".join(f"{idx + 1}. {heading}" for idx, heading in enumerate(sections))
    results: list[SectionWriteResult | None] = [None] * len(sections)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _write_one_section,
                request,
                plan,
                sections=sections,
                section_heading=heading,
                section_index=index,
                outline=outline,
                sources=sources,
                evidence=evidence,
                research_answer=research_answer,
                repair_instruction=repair_instruction,
                timeout_s=timeout_s,
            )
            for index, heading in enumerate(sections)
        ]
        for future in as_completed(futures):
            result = future.result()
            results[result.index] = result

    for result in results:
        if result is None:
            continue
        written_sections.append(result.markdown)
        model_used = result.model_used or model_used
        latency_ms += result.latency_ms
        cost_usd += result.cost_usd
        attempted_models.extend([model for model in result.attempted_models if model not in attempted_models])
        failed_model_attempts.extend(result.failed_model_attempts)
    return DocumentDraft(
        markdown=f"# {plan.title.strip() or 'Document'}\n\n" + "\n\n".join(written_sections).strip(),
        model_used=model_used,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model_role="document_writer",
        preferred_model=model_client.model_for_role("document_writer", quality_mode=request.quality_mode, overrides=request.model_overrides) or "",
        attempted_models=attempted_models,
        failed_model_attempts=failed_model_attempts,
    )


def _write_one_section(
    request: TurnRequest,
    plan: DocumentPlan,
    *,
    sections: list[str],
    section_heading: str,
    section_index: int,
    outline: str,
    sources: list[Source],
    evidence: EvidencePack | None,
    research_answer: str | None,
    repair_instruction: str | None,
    timeout_s: int,
) -> SectionWriteResult:
    depth = _section_depth(section_heading, index=section_index, total=len(sections), request=request)
    context = _section_source_context(section_heading, evidence=evidence, sources=sources, max_chars=_section_context_chars(depth))
    prompt = _section_writer_prompt(
        request,
        plan,
        section_heading=section_heading,
        section_index=section_index,
        section_depth=depth,
        outline=outline,
        research_answer=research_answer,
        source_context_text=context,
        prior_context=_neighbor_section_context(sections, section_index),
        repair_instruction=repair_instruction,
    )
    system_prompt = resolve_prompt(
        "agent.document.section_write.default",
        agent_id="document_section_writer",
        fallback_system_prompt="You are the Fronei document section writer. Write only the requested section in markdown.",
        variables=["section", "outline", "research_answer", "source_context"],
        profile=infer_research_profile(request.message),
    )
    response = model_client.simple_completion(
        system_prompt.system_prompt,
        prompt,
        max_tokens=_section_token_budget(depth, request=request),
        role="document_writer",
        quality_mode=request.quality_mode,
        overrides=request.model_overrides,
        timeout_s=timeout_s,
    )
    return SectionWriteResult(
        index=section_index,
        markdown=_normalize_section_markdown(section_heading, response.text, section_number=section_index + 1),
        model_used=response.model_used,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
        attempted_models=list(response.attempted_models),
        failed_model_attempts=list(response.failed_model_attempts),
    )


def _document_writer_concurrency(section_count: int) -> int:
    configured = int(get_settings().document_writer_concurrency or 1)
    return max(1, min(section_count, configured))


def _neighbor_section_context(sections: list[str], index: int) -> str:
    hints: list[str] = []
    if index > 0:
        hints.append(f"Previous planned section: {sections[index - 1]}")
    if index + 1 < len(sections):
        hints.append(f"Next planned section: {sections[index + 1]}")
    return "\n".join(hints)


def judge_document(draft: DocumentDraft, plan: DocumentPlan, *, source_count: int) -> DocumentJudgeResult:
    markdown = draft.markdown.strip()
    issues: list[str] = []
    min_chars = _minimum_document_chars(plan)
    if len(markdown) < min_chars:
        issues.append("Document is too short for the requested artifact.")
    headings = re.findall(r"^#{1,3}\s+.+$", markdown, flags=re.MULTILINE)
    min_headings = _minimum_document_headings(plan)
    if len(headings) < min_headings:
        issues.append("Document needs clearer section headings.")
    if source_count and "[S" not in markdown:
        issues.append("Research-backed document should include source citations.")
    if not issues:
        return DocumentJudgeResult(status="pass", score=0.92)
    return DocumentJudgeResult(
        status="repair",
        score=max(0.3, 0.86 - 0.16 * len(issues)),
        issues=issues,
        repair_instruction=" ".join(issues),
    )


def choose_artifact_tool(request: TurnRequest, plan: DocumentPlan) -> str:
    if request.output_format == "markdown" or plan.format == "markdown":
        return "make_markdown_artifact"
    if request.output_format == "pptx" or plan.format == "pptx":
        return "make_pptx_artifact"
    return "make_docx_artifact"


def _document_writer_token_budget(request: TurnRequest, *, research_answer: str | None = None) -> int:
    profile = infer_research_profile(request.message)
    is_deep = request.research_level == "deep"
    is_exec = request.quality_mode == "executive"
    if profile == "technical_architecture" and is_deep and research_answer:
        return 12000 if is_exec else 10000
    if profile == "technical_architecture" and research_answer:
        return 9000 if is_exec else 7600
    if profile == "vendor_comparison" and research_answer:
        return (10000 if is_deep else 7500) if is_exec else (8500 if is_deep else 6000)
    if profile == "market_landscape" and research_answer:
        return (9000 if is_deep else 7000) if is_exec else (7500 if is_deep else 5500)
    if profile == "policy_regulatory" and research_answer:
        return (9000 if is_deep else 6500) if is_exec else (7500 if is_deep else 5500)
    if profile == "strategy_brief" and research_answer:
        return (7000 if is_deep else 4500) if is_exec else (6000 if is_deep else 3500)
    if profile == "implementation_plan" and research_answer:
        return (9000 if is_deep else 6500) if is_exec else (7500 if is_deep else 5500)
    if research_answer and ("report" in request.message.lower() or request.output_format in {"docx", "markdown", "pptx"}):
        return 7200 if is_exec else 6000
    return 2600 if is_exec else 2200


def _document_writer_instruction(request: TurnRequest, *, research_answer: str | None = None) -> str:
    profile = infer_research_profile(request.message)
    is_deep = request.research_level == "deep"
    if profile == "technical_architecture" and is_deep and research_answer:
        if request.output_format == "pptx":
            return (
                "Write a structured slide deck plan in markdown. Do not write an essay. "
                "Start with an H1 deck title, then use one H2 per slide. For each slide, include: "
                "one assertion-style slide title, 2-3 sparse bullets, optional table/chart data when useful, "
                "and speaker notes with the deeper explanation and citations. Every slide should have a clear "
                "visual job such as compare, map architecture, show workflow, quantify impact, or recommend action."
            )
        return (
            "Write the complete document body in markdown. Do not summarize the research into a short memo. "
            "Produce a deep technical report with 10-14 substantive sections, source-backed implementation detail, "
            "named systems/examples, architecture patterns, workflow/control-flow explanations, data/state models, "
            "tool and model boundaries, guardrails, observability, budget/latency trade-offs, failure modes, and "
            "practical design recommendations. Include compact tables or text diagrams when they clarify the architecture. "
            "Use citations from the research summary and sources throughout."
        )
    if profile == "vendor_comparison" and research_answer:
        if request.output_format == "pptx":
            return (
                "Write a structured vendor-comparison slide deck plan in markdown. Do not write a report. "
                "Use one H2 per slide, sparse visible bullets, a comparison table slide, recommendation slide, "
                "risk/switching-cost slide, and speaker notes with cited detail."
            )
        return (
            "Write the complete document body in markdown. "
            "Open with a 1-paragraph executive summary naming the top recommendation and its key differentiator. "
            "Evaluate each vendor/option with consistent criteria: pricing, API capabilities, security/compliance, "
            "SLAs, use-case fit, vendor risk, and switching cost. "
            "Include a comparison table where the criteria and vendors intersect. "
            "Close with a scored or ranked recommendation matrix and clear rationale. "
            "Every factual claim must have a [S#] citation."
        )
    if request.output_format == "pptx" or _presentation_requested(request.message):
        return (
            "Write a structured slide deck plan in markdown. Do not write a prose report. "
            "Start with an H1 deck title, then use one H2 per slide. Keep visible slide text sparse: "
            "2-4 concise bullets, compact tables where useful, and no long paragraphs on slides. "
            "For each slide, include speaker notes with the deeper explanation, evidence, citations, and presenter talking points. "
            "Make the deck flow from context to findings, implications, recommendations, and next steps."
        )
    if profile == "market_landscape" and research_answer:
        return (
            "Write the complete document body in markdown. "
            "Open with a 1-paragraph market framing: what the space is, why it matters now, and the key dynamic. "
            "Cover: market segmentation, key players with positioning, quantitative size/growth metrics, "
            "technology and product trends, business model patterns, buyer behavior, and competitive dynamics. "
            "Use tables for player comparisons and metric summaries. "
            "Close with business implications for a buyer, investor, or entrant. "
            "All metrics and claims must be [S#] cited."
        )
    if profile == "policy_regulatory" and research_answer:
        return (
            "Write the complete document body in markdown. "
            "Lead with a plain-language summary of the primary obligation and who it applies to. "
            "For each regulation: name the authoritative source, enforcement body, jurisdiction, effective date, "
            "and specific compliance requirements. "
            "Cover penalties with [S#] citations to actual enforcement actions. "
            "Distinguish binding requirements from guidance/safe-harbor. "
            "Flag pending changes. Keep jurisdictions clearly separated. "
            "Close with a compliance action checklist: what to do, by when."
        )
    if profile == "strategy_brief" and research_answer:
        return (
            "Write the complete document body in markdown. "
            "Open with a 1-paragraph executive summary: the decision, the recommendation, and the key rationale. "
            "Then: business context and problem with evidence; 2-4 strategic options with trade-offs; "
            "recommended option with rationale and risk acknowledgment; top 3-5 risks with mitigations; "
            "resource, cost, and timeline implications; success metrics. "
            "Close with next steps: owner, action, deadline. "
            "Use crisp, decision-grade language. Every factual claim must have a [S#] citation."
        )
    if profile == "implementation_plan" and research_answer:
        return (
            "Write the complete document body in markdown. "
            "Open with scope, objectives, and measurable success criteria. "
            "Use tables for: workstream breakdown (workstream, tasks, dependencies, owner, timeline), "
            "milestone tracker (milestone, target date, owner, status), "
            "and risk register (risk, likelihood, impact, mitigation, owner). "
            "Include governance, communication cadence, and change management. "
            "Close with a go/no-go checklist for the first milestone gate. "
            "Ground planning assumptions in [S#] cited evidence where available."
        )
    if research_answer and ("report" in request.message.lower() or request.output_format in {"docx", "markdown"}):
        return (
            "Write the complete document body in markdown. Produce a substantial report with clear headings, "
            "evidence-backed findings, specific examples, caveats, and recommendations. Avoid compressing the output "
            "into a brief summary unless the user explicitly asked for concision."
        )
    return "Write the complete document body in markdown. Use clear headings and complete, useful paragraphs."


_BY_SECTION_PROFILES = {
    "technical_architecture",
    "vendor_comparison",
    "market_landscape",
    "policy_regulatory",
    "implementation_plan",
    # strategy_brief is intentionally excluded — it's a concise brief, not a long document
}


def _should_write_by_section(
    request: TurnRequest,
    plan: DocumentPlan,
    *,
    research_answer: str | None = None,
) -> bool:
    if not research_answer:
        return False
    if request.research_level != "deep":
        return False
    if len(plan.sections or []) < 6:
        return False
    profile = infer_research_profile(request.message)
    if profile in _BY_SECTION_PROFILES:
        if request.output_format == "pptx" or plan.format == "pptx":
            return False
        return True
    if request.output_format == "pptx" or plan.format == "pptx":
        return False
    return "report" in request.message.lower() or request.output_format in {"docx", "markdown"}


def _section_writer_prompt(
    request: TurnRequest,
    plan: DocumentPlan,
    *,
    section_heading: str,
    section_index: int,
    section_depth: str,
    outline: str,
    research_answer: str | None,
    source_context_text: str,
    prior_context: str,
    repair_instruction: str | None,
) -> str:
    word_target = {
        "brief": "250-450 words",
        "standard": "500-850 words",
        "deep": "900-1400 words",
    }.get(section_depth, "500-850 words")
    parts = [
        f"User request:\n{request.message}",
        f"Document title: {plan.title}",
        f"Audience: {plan.audience}",
        f"Full outline:\n{outline}",
        f"Current section {section_index + 1}/{len(plan.sections or [])}: {section_heading}",
        f"Depth: {section_depth}; target length: {word_target}.",
        (
            "Write this section only. Use the exact section heading as a markdown H2. "
            "Vary depth based on evidence. Include concrete architecture mechanisms, named examples, "
            "data/state objects, control flow, trade-offs, and failure modes when relevant. "
            "Use [S#] citations for evidence-backed factual claims. Do not add a global conclusion unless this is the final section."
        ),
    ]
    if repair_instruction:
        parts.append(f"Repair instruction:\n{repair_instruction}")
    if prior_context:
        parts.append(f"Previous section tail for continuity:\n{prior_context}")
    if research_answer:
        parts.append(f"Research synthesis excerpt:\n{_section_research_excerpt(section_heading, research_answer)}")
    parts.append(f"Relevant sources/evidence:\n{source_context_text or 'No targeted source context available; disclose any evidence gap plainly.'}")
    return "\n\n".join(parts)


def _section_depth(heading: str, *, index: int, total: int, request: TurnRequest) -> str:
    lower = heading.lower()
    profile = infer_research_profile(request.message)
    if index == 0 or any(token in lower for token in ("executive summary", "introduction", "scope", "overview")):
        return "brief"
    if index == total - 1 or any(token in lower for token in ("recommendation", "conclusion", "summary", "next steps", "checklist")):
        return "standard"
    # Profile-specific deep-section triggers
    deep_terms_by_profile: dict[str, tuple[str, ...]] = {
        "technical_architecture": (
            "architecture", "workflow", "orchestration", "agent", "llm", "integration",
            "evidence", "memory", "state", "tool", "render", "verification", "reflection",
            "guardrail", "failure", "security", "trade-off", "cost", "latency",
        ),
        "vendor_comparison": (
            "pricing", "api", "capabilities", "security", "compliance", "sla", "reliability",
            "use-case", "fit", "lock-in", "migration", "switching",
        ),
        "market_landscape": (
            "players", "competitive", "market size", "growth", "trends", "business model",
            "adoption", "barriers", "buyer", "segment",
        ),
        "policy_regulatory": (
            "regulation", "compliance", "enforcement", "jurisdiction", "obligation",
            "penalty", "guidance", "safe-harbor", "pending",
        ),
        "implementation_plan": (
            "workstream", "milestone", "dependencies", "risk", "resource", "owner",
            "timeline", "governance", "rollback", "contingency",
        ),
    }
    deep_terms = deep_terms_by_profile.get(profile, (
        "architecture", "workflow", "integration", "evidence", "security", "trade-off",
    ))
    if request.research_level == "deep" and any(term in lower for term in deep_terms):
        return "deep"
    return "standard"


def _section_token_budget(depth: str, *, request: TurnRequest) -> int:
    executive_bonus = 500 if request.quality_mode == "executive" else 0
    if depth == "deep":
        return 2400 + executive_bonus
    if depth == "standard":
        return 1500 + executive_bonus
    return 900 + executive_bonus


def _section_context_chars(depth: str) -> int:
    if depth == "deep":
        return 9000
    if depth == "standard":
        return 5500
    return 3000


def _section_source_context(
    heading: str,
    *,
    evidence: EvidencePack | None,
    sources: list[Source],
    max_chars: int,
) -> str:
    if evidence is not None and evidence.items:
        terms = _section_terms(heading)
        scored = []
        for item in evidence.items:
            text = f"{item.title} {item.question} {item.evidence}".lower()
            score = sum(1 for term in terms if term in text)
            if score <= 0:
                score = 1 if any(term in text for term in ("architecture", "agent", "workflow", "system")) else 0
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chunks: list[str] = []
        remaining = max_chars
        for score, item in scored[:8]:
            if score <= 0 and chunks:
                continue
            chunk = (
                f"[{item.source_id}] {item.title}\n"
                f"Question: {item.question}\n"
                f"URL: {item.url}\n"
                f"Evidence: {item.evidence[:1800]}"
            )
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
        return "\n\n".join(chunks)
    context = source_context(sources)
    return context[:max_chars]


def _section_research_excerpt(heading: str, research_answer: str) -> str:
    terms = _section_terms(heading)
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", research_answer or "") if paragraph.strip()]
    scored = []
    for paragraph in paragraphs:
        lower = paragraph.lower()
        scored.append((sum(1 for term in terms if term in lower), paragraph))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected = [paragraph for score, paragraph in scored[:4] if score > 0] or paragraphs[:2]
    return "\n\n".join(selected)[:5000]


def _section_terms(heading: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9.]{3,}", heading.lower())
        if token not in {"the", "and", "for", "with", "from", "section", "overview"}
    ]


def _normalize_section_markdown(heading: str, markdown: str, *, section_number: int) -> str:
    text = (markdown or "").strip()
    heading_text = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading.strip()).strip()
    canonical_heading = f"## {section_number}. {heading_text}"
    if not text:
        return f"{canonical_heading}\n\nNo evidence-backed content was generated for this section."
    lines = text.splitlines()
    normalized: list[str] = [canonical_heading]
    subheading_index = 0
    skipped_first_heading = False
    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            if not skipped_first_heading:
                skipped_first_heading = True
                continue
            subheading_index += 1
            title = re.sub(r"^#{1,6}\s+", "", line).strip()
            title = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", title).strip()
            if title:
                normalized.append(f"### {section_number}.{subheading_index} {title}")
            continue
        normalized.append(line)
    body = "\n".join(normalized).strip()
    return body or f"{canonical_heading}\n\nNo evidence-backed content was generated for this section."


def _minimum_document_chars(plan: DocumentPlan) -> int:
    section_count = len(plan.sections or [])
    if section_count >= 8:
        return 8000
    if section_count >= 5:
        return 4500
    return 900


def _minimum_document_headings(plan: DocumentPlan) -> int:
    section_count = len(plan.sections or [])
    if section_count >= 8:
        return 8
    return max(2, min(5, section_count))


def build_artifact(
    tool_registry,
    plan: DocumentPlan,
    draft: DocumentDraft,
    tool_name: str,
    request: TurnRequest | None = None,
    *,
    user_id: str | None = None,
) -> tuple[Artifact, ToolCall]:
    inputs: dict[str, object] = {"title": plan.title, "markdown": draft.markdown}
    if tool_name == "make_docx_artifact":
        inputs["expected_sections"] = list(plan.sections or [])
    if tool_name == "make_pptx_artifact":
        inputs["expected_slides"] = list(plan.sections or [])
        if request is not None and request.template_id:
            inputs["template_id"] = request.template_id
        if user_id:
            inputs["user_id"] = user_id
    artifact, call = tool_registry.run(tool_name, inputs)
    if call.ok and artifact is not None:
        return artifact, call
    fallback, fallback_call = tool_registry.run(
        "make_markdown_artifact",
        {"title": plan.title, "markdown": draft.markdown},
    )
    fallback_call.input["fallback_from"] = tool_name
    fallback_call.input["fallback_reason"] = call.error
    return fallback, fallback_call


def _fallback_plan(request: TurnRequest) -> DocumentPlan:
    return DocumentPlan(
        title=_title_from_message(request.message),
        format=_fallback_format(request),
        sections=["Executive summary", "Key findings", "Recommended next steps"],
        source="heuristic",
    )


def _normalize_plan(plan: DocumentPlan, request: TurnRequest) -> DocumentPlan:
    if not plan.title:
        plan.title = _title_from_message(request.message)
    if request.output_format == "markdown":
        plan.format = "markdown"
    elif request.output_format == "pptx" or _presentation_requested(request.message):
        plan.format = "pptx"
    elif request.output_format == "docx" or "docx" in request.message.lower():
        plan.format = "docx"
    if not plan.sections:
        plan.sections = ["Executive summary", "Key findings", "Recommended next steps"]
    plan.sections = _dedupe(plan.sections)[: _section_limit(request)]
    return plan


def _planner_research_summary(request: TurnRequest, research_answer: str | None) -> str:
    if not research_answer:
        return ""
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep":
        return research_answer[:12000]
    if request.research_level == "deep":
        return research_answer[:8000]
    return research_answer[:3000]


def _section_guidance(request: TurnRequest, *, research_answer: str | None = None) -> str:
    profile = infer_research_profile(request.message)
    if request.output_format == "pptx" or _presentation_requested(request.message):
        return "Use 8-12 slides. Each section becomes a slide with sparse visible text and speaker-note depth."
    if profile == "technical_architecture" and request.research_level == "deep" and research_answer:
        return "Use 10-14 sections. Preserve implementation detail; do not compress the research into an executive memo."
    if research_answer and request.research_level == "deep":
        return "Use 7-10 sections for the deep research report."
    return "Use 4-7 sections."


def _section_limit(request: TurnRequest) -> int:
    profile = infer_research_profile(request.message)
    is_deep = request.research_level == "deep"
    if request.output_format == "pptx" or _presentation_requested(request.message):
        return 14 if is_deep else 10
    # strategy_brief is intentionally compact even at deep level
    if profile == "strategy_brief":
        return 8 if is_deep else 6
    if profile == "technical_architecture" and is_deep:
        return 14
    if profile in {"vendor_comparison", "market_landscape", "policy_regulatory", "implementation_plan"} and is_deep:
        return 12
    if is_deep:
        return 10
    return 7


def _fallback_format(request: TurnRequest) -> Literal["markdown", "docx", "pptx"]:
    if request.output_format in {"markdown", "docx", "pptx"}:
        return request.output_format
    if _presentation_requested(request.message):
        return "pptx"
    return "docx"


def _presentation_requested(message: str) -> bool:
    text = (message or "").lower()
    return any(term in text for term in ("pptx", "powerpoint", "presentation", "slides", "slide deck", "deck"))


def _title_from_message(message: str) -> str:
    cleaned = " ".join(message.replace("\n", " ").split())
    return cleaned[:80].strip(" .") or "Fronei document"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


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
