from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

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


class DocumentJudgeResult(BaseModel):
    status: Literal["pass", "repair"] = "pass"
    score: float = 1.0
    issues: list[str] = Field(default_factory=list)
    repair_instruction: str = ""


PLAN_PROMPT = """You are the Agent v3 document planner.

Create a compact document plan. Return only JSON:
{
  "title": "short document title",
  "format": "markdown|docx",
  "audience": "intended audience",
  "sections": ["4-7 concrete section headings"]
}
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
                            "research_summary": (research_answer or "")[:2000],
                            "source_count": len(sources),
                            "evidence_count": len(evidence.items) if evidence else 0,
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
    prompt += "Write the complete document body in markdown. Use clear headings and concise paragraphs."
    response = model_client.simple_completion(
        "You are the Agent v3 document writer. Produce only the document body in markdown.",
        prompt,
        max_tokens=_document_writer_token_budget(request, research_answer=research_answer),
        role="document_writer",
        quality_mode=request.quality_mode,
    )
    return DocumentDraft(
        markdown=response.text,
        model_used=response.model_used,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
    )


def judge_document(draft: DocumentDraft, plan: DocumentPlan, *, source_count: int) -> DocumentJudgeResult:
    markdown = draft.markdown.strip()
    issues: list[str] = []
    if len(markdown) < 300:
        issues.append("Document is too short for the requested artifact.")
    headings = re.findall(r"^#{1,3}\s+.+$", markdown, flags=re.MULTILINE)
    if len(headings) < max(2, min(4, len(plan.sections))):
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
    if profile == "technical_architecture" and research_answer:
        return 7000 if request.quality_mode == "executive" else 5600
    if research_answer and ("report" in request.message.lower() or request.output_format in {"docx", "markdown"}):
        return 5200 if request.quality_mode == "executive" else 4000
    return 2600 if request.quality_mode == "executive" else 2200


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
    plan.sections = _dedupe(plan.sections)[:7]
    return plan


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
