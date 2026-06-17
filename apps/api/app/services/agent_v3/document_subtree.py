from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.agent_v3 import model_client
from app.services.agent_v3.models import AgentV3Request, Artifact, Source, ToolCall
from app.services.agent_v3.research_subtree import EvidencePack, infer_research_profile, source_context_from_evidence
from app.services.agent_v3.tools import source_context

logger = logging.getLogger(__name__)


class DocumentPlan(BaseModel):
    title: str = "Agent v3 document"
    format: Literal["markdown", "docx"] = "docx"
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


PLAN_PROMPT = """You are the Agent v3 document planner.

Create a document plan sized to the user request. Return only JSON:
{
  "title": "short document title",
  "format": "markdown|docx",
  "audience": "intended audience",
  "sections": ["concrete section headings"]
}
For ordinary documents, use 4-7 sections. For deep technical reports, use 10-14
substantive sections that preserve the important architecture, implementation,
evidence, trade-off, failure-mode, and recommendation areas from the research.
Prefer docx when the user asks for a downloadable report/document.
"""


def plan_document(
    request: AgentV3Request,
    *,
    sources: list[Source],
    research_answer: str | None = None,
    evidence: EvidencePack | None = None,
) -> DocumentPlan:
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": PLAN_PROMPT},
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
        logger.warning("agent_v3 document planning failed; using fallback plan: %s", exc)
        plan = _fallback_plan(request)
        plan.fallback_reason = str(exc)
        return plan


def write_document(
    request: AgentV3Request,
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
    request: AgentV3Request,
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
    response = model_client.simple_completion(
        "You are the Agent v3 document writer. Produce only the document body in markdown.",
        prompt,
        max_tokens=_document_writer_token_budget(request, research_answer=research_answer),
        role="document_writer",
        quality_mode=request.quality_mode,
        timeout_s=max(30, int(get_settings().agent_v3_longform_timeout_s or 180)),
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
    request: AgentV3Request,
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
    timeout_s = max(30, int(get_settings().agent_v3_longform_timeout_s or 180))
    outline = "\n".join(f"{idx + 1}. {heading}" for idx, heading in enumerate(sections))
    prior_context = ""
    for index, heading in enumerate(sections):
        depth = _section_depth(heading, index=index, total=len(sections), request=request)
        context = _section_source_context(heading, evidence=evidence, sources=sources, max_chars=_section_context_chars(depth))
        prompt = _section_writer_prompt(
            request,
            plan,
            section_heading=heading,
            section_index=index,
            section_depth=depth,
            outline=outline,
            research_answer=research_answer,
            source_context_text=context,
            prior_context=prior_context,
            repair_instruction=repair_instruction,
        )
        response = model_client.simple_completion(
            "You are the Agent v3 document section writer. Write only the requested section in markdown.",
            prompt,
            max_tokens=_section_token_budget(depth, request=request),
            role="document_writer",
            quality_mode=request.quality_mode,
            timeout_s=timeout_s,
        )
        section_md = _normalize_section_markdown(heading, response.text, section_number=index + 1)
        written_sections.append(section_md)
        prior_context = section_md[-1800:]
        model_used = response.model_used or model_used
        latency_ms += response.latency_ms
        cost_usd += response.cost_usd
        attempted_models.extend([model for model in response.attempted_models if model not in attempted_models])
        failed_model_attempts.extend(response.failed_model_attempts)
    return DocumentDraft(
        markdown=f"# {plan.title.strip() or 'Document'}\n\n" + "\n\n".join(written_sections).strip(),
        model_used=model_used,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model_role="document_writer",
        preferred_model=model_client.model_for_role("document_writer", quality_mode=request.quality_mode) or "",
        attempted_models=attempted_models,
        failed_model_attempts=failed_model_attempts,
    )


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


def choose_artifact_tool(request: AgentV3Request, plan: DocumentPlan) -> str:
    if request.output_format == "markdown" or plan.format == "markdown":
        return "make_markdown_artifact"
    return "make_docx_artifact"


def _document_writer_token_budget(request: AgentV3Request, *, research_answer: str | None = None) -> int:
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep" and research_answer:
        return 12000 if request.quality_mode == "executive" else 10000
    if profile == "technical_architecture" and research_answer:
        return 9000 if request.quality_mode == "executive" else 7600
    if research_answer and ("report" in request.message.lower() or request.output_format in {"docx", "markdown"}):
        return 7200 if request.quality_mode == "executive" else 6000
    return 2600 if request.quality_mode == "executive" else 2200


def _document_writer_instruction(request: AgentV3Request, *, research_answer: str | None = None) -> str:
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep" and research_answer:
        return (
            "Write the complete document body in markdown. Do not summarize the research into a short memo. "
            "Produce a deep technical report with 10-14 substantive sections, source-backed implementation detail, "
            "named systems/examples, architecture patterns, workflow/control-flow explanations, data/state models, "
            "tool and model boundaries, guardrails, observability, budget/latency trade-offs, failure modes, and "
            "practical design recommendations. Include compact tables or text diagrams when they clarify the architecture. "
            "Use citations from the research summary and sources throughout."
        )
    if research_answer and ("report" in request.message.lower() or request.output_format in {"docx", "markdown"}):
        return (
            "Write the complete document body in markdown. Produce a substantial report with clear headings, "
            "evidence-backed findings, specific examples, caveats, and recommendations. Avoid compressing the output "
            "into a brief summary unless the user explicitly asked for concision."
        )
    return "Write the complete document body in markdown. Use clear headings and complete, useful paragraphs."


def _should_write_by_section(
    request: AgentV3Request,
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
    return "report" in request.message.lower() or request.output_format in {"docx", "markdown"}


def _section_writer_prompt(
    request: AgentV3Request,
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


def _section_depth(heading: str, *, index: int, total: int, request: AgentV3Request) -> str:
    lower = heading.lower()
    if index == 0 or any(token in lower for token in ("executive summary", "introduction", "scope")):
        return "brief"
    if index == total - 1 or any(token in lower for token in ("recommendation", "conclusion", "summary")):
        return "standard"
    deep_terms = (
        "architecture",
        "workflow",
        "orchestration",
        "agent",
        "llm",
        "integration",
        "evidence",
        "memory",
        "state",
        "tool",
        "render",
        "verification",
        "reflection",
        "guardrail",
        "failure",
        "security",
        "trade-off",
        "cost",
        "latency",
    )
    if request.research_level == "deep" and any(term in lower for term in deep_terms):
        return "deep"
    return "standard"


def _section_token_budget(depth: str, *, request: AgentV3Request) -> int:
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
    heading_text = heading.strip()
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


def build_artifact(tool_registry, plan: DocumentPlan, draft: DocumentDraft, tool_name: str) -> tuple[Artifact, ToolCall]:
    artifact, call = tool_registry.run(tool_name, {"title": plan.title, "markdown": draft.markdown})
    if call.ok and artifact is not None:
        return artifact, call
    fallback, fallback_call = tool_registry.run(
        "make_markdown_artifact",
        {"title": plan.title, "markdown": draft.markdown},
    )
    fallback_call.input["fallback_from"] = tool_name
    fallback_call.input["fallback_reason"] = call.error
    return fallback, fallback_call


def _fallback_plan(request: AgentV3Request) -> DocumentPlan:
    return DocumentPlan(
        title=_title_from_message(request.message),
        format="markdown" if request.output_format == "markdown" else "docx",
        sections=["Executive summary", "Key findings", "Recommended next steps"],
        source="heuristic",
    )


def _normalize_plan(plan: DocumentPlan, request: AgentV3Request) -> DocumentPlan:
    if not plan.title:
        plan.title = _title_from_message(request.message)
    if request.output_format == "markdown":
        plan.format = "markdown"
    elif request.output_format == "docx" or "docx" in request.message.lower():
        plan.format = "docx"
    if not plan.sections:
        plan.sections = ["Executive summary", "Key findings", "Recommended next steps"]
    plan.sections = _dedupe(plan.sections)[: _section_limit(request)]
    return plan


def _planner_research_summary(request: AgentV3Request, research_answer: str | None) -> str:
    if not research_answer:
        return ""
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep":
        return research_answer[:12000]
    if request.research_level == "deep":
        return research_answer[:8000]
    return research_answer[:3000]


def _section_guidance(request: AgentV3Request, *, research_answer: str | None = None) -> str:
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep" and research_answer:
        return "Use 10-14 sections. Preserve implementation detail; do not compress the research into an executive memo."
    if research_answer and request.research_level == "deep":
        return "Use 7-10 sections for the deep research report."
    return "Use 4-7 sections."


def _section_limit(request: AgentV3Request) -> int:
    profile = infer_research_profile(request.message)
    if profile == "technical_architecture" and request.research_level == "deep":
        return 14
    if request.research_level == "deep":
        return 10
    return 7


def _title_from_message(message: str) -> str:
    cleaned = " ".join(message.replace("\n", " ").split())
    return cleaned[:80].strip(" .") or "Agent v3 document"


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
