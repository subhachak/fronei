import base64
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, CurrentUserIsAdmin
from app.config import get_settings
from app.db.models import (
    RequestLog,
    SessionLocal,
    get_effective_monthly_budget,
    get_monthly_spend,
    is_user_pending,
    is_user_suspended,
)
from app.schemas import (
    DocumentExtractResponse,
    DocumentGenerateFromPromptRequest,
    DocumentGenerateFromPromptResponse,
    DocumentGenerateRequest,
)
from app.services.budget_guard import enforce_global_monthly_budget
from app.services.chat_pipeline import _build_doc_context
from app.services.document_extractor import (
    MAX_PDF_PAGES,
    SUPPORTED,
    ExtractionError,
    extract_text,
)
from app.services.document_generator import generate_docx_bytes, generate_xlsx_bytes
from app.services.llm_gateway import invoke_llm
from app.services.personal_context import build_context
from app.services.planner import run_planner
from app.services.rate_limit import check_rate_limit, rate_limiter
from app.services.research_orchestrator import run_research
from app.services.router import choose_route
from app.services.web_context import gather_web_context

router = APIRouter(prefix="/documents", tags=["documents"])
MAX_UPLOAD_BYTES = 30 * 1024 * 1024   # 30 MB
DOC_TYPES = {
    "executive_report",
    "proposal",
    "memo",
    "technical_spec",
    "meeting_notes",
    "one_pager",
    "letter",
    "resume",
}
DOCUMENT_SYSTEM_PROMPT = """You are Fronei's document generation engine.

Write a polished, client-presentable document from the user's request.

Rules:
- Output only the document body in clean Markdown. Do not include commentary about generating the document.
- Start with a strong H1 title unless the user explicitly asks for a different format.
- Use professional, client-ready language.
- Include useful sections, headings, bullets, tables, and next steps when appropriate.
- Use Markdown headings, tables for comparative or numeric data, and bold for key terms.
- Make assumptions explicit when the prompt is underspecified.
- Keep the document coherent enough to paste directly into a Word document.
- Do not invent precise facts, metrics, dates, legal claims, or citations not supplied by the user.
- If source-grounded research or web context is provided, use it as source material and preserve useful citations.
"""
DOC_TYPE_PROMPTS = {
    "executive_report": """Document type: executive_report
Expected structure:
- H1 title
- Executive summary
- Situation / background
- Analysis, using tables where the information is data-heavy or comparative
- Recommendations
- Risks and mitigations
- Next steps
Use concise, decision-oriented language suitable for clients or senior stakeholders.""",
    "proposal": """Document type: proposal
Expected structure:
- H1 title
- Problem statement
- Proposed approach
- Scope and timeline
- Cost / ROI, using tables where helpful
- Terms, assumptions, or dependencies
- Next steps
Keep the tone confident, practical, and commercially credible.""",
    "memo": """Document type: memo
Expected structure:
- H1 title
- Header block with To, From, Date, and Re
- Purpose
- Body
- Action items
Keep it concise, direct, and easy to skim.""",
    "technical_spec": """Document type: technical_spec
Expected structure:
- H1 title
- Overview
- Architecture
- Requirements
- Implementation notes
- Risks / constraints
- Open questions
Use precise technical language and tables for requirements, interfaces, or trade-offs.""",
    "meeting_notes": """Document type: meeting_notes
Expected structure:
- H1 title
- Attendees
- Agenda
- Discussion summary
- Decisions
- Action items with owners and due dates when available
Do not invent attendees, owners, or dates that were not provided.""",
    "one_pager": """Document type: one_pager
Expected structure:
- H1 headline
- 3-5 key points
- Supporting facts or rationale
- Single call-to-action
Keep it tight enough to fit on one page.""",
    "letter": """Document type: letter
Expected structure:
- Date
- Recipient / salutation when provided
- Opening purpose
- Body
- Closing and signature placeholder
Use polished, professional letter language.""",
    "resume": """Document type: resume
Expected structure:
- H1 with the person's name
- Contact line (location, email, phone, LinkedIn if provided)
- Professional summary (2-4 sentences)
- Work experience, most recent first, each with company, title, location, dates, and bullet achievements
- Skills, grouped by category
- Certifications (if provided)
- Education
Use concise, achievement-oriented bullets (action verb + result, quantify where possible). Do not invent \
employers, dates, titles, or credentials not supplied by the user.""",
}


@router.post(
    "/extract",
    response_model=DocumentExtractResponse,
    dependencies=[rate_limiter("documents", "rate_limit_documents_per_minute", 60)],
)
async def extract_document(
    file: UploadFile = File(...),
    user_id: str = CurrentUser,
) -> DocumentExtractResponse:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 30 MB).")
    filename = file.filename or "upload"
    try:
        text, pages_extracted, truncated, method = extract_text(filename, content)
    except ExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    suffix = Path(filename).suffix.lower()
    pages_total = pages_extracted  # for non-PDF
    if suffix == ".pdf":
        try:
            import fitz
            doc = fitz.open(stream=content, filetype="pdf")
            pages_total = len(doc)
            doc.close()
        except Exception:
            pages_total = pages_extracted

    return DocumentExtractResponse(
        name=filename,
        char_count=len(text),
        pages_extracted=pages_extracted,
        pages_total=pages_total,
        truncated=truncated,
        method=method,
        text=text,
        text_preview=text[:300],
    )


@router.get("/supported")
def supported_types() -> dict:
    return {
        "types": sorted(SUPPORTED),
        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_pdf_pages": MAX_PDF_PAGES,
    }


@router.post(
    "/generate/docx",
    dependencies=[rate_limiter("document_generation", "rate_limit_documents_per_minute", 60)],
)
def generate_docx(
    req: DocumentGenerateRequest,
    user_id: str = CurrentUser,
) -> StreamingResponse:
    content = generate_docx_bytes(req.title, req.content, req.subtitle)
    filename = f"{_safe_filename(req.title)}.docx"
    quoted = quote(filename)
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/generate/from-prompt/docx",
    dependencies=[rate_limiter("document_generation", "rate_limit_documents_per_minute", 60)],
    response_model=DocumentGenerateFromPromptResponse,
)
def generate_docx_from_prompt(
    req: DocumentGenerateFromPromptRequest,
    user_id: str = CurrentUser,
    is_admin: bool = CurrentUserIsAdmin,
) -> DocumentGenerateFromPromptResponse:
    settings = get_settings()
    db = SessionLocal()
    route = None
    try:
        if is_user_suspended(db, user_id):
            raise HTTPException(status_code=403, detail="This account is suspended.")
        if is_user_pending(db, user_id):
            raise HTTPException(status_code=403, detail="Your account is pending admin approval.")
        enforce_global_monthly_budget(db, is_admin)
        if not is_admin:
            monthly_spend = get_monthly_spend(db, user_id)
            monthly_budget = get_effective_monthly_budget(db, user_id)
            if monthly_spend >= monthly_budget:
                raise HTTPException(
                    status_code=429,
                    detail=f"Monthly budget of ${monthly_budget:.2f} reached "
                           f"(spent ${monthly_spend:.4f} this month). Ask an admin to adjust the limit.",
                )

        doc_context = _build_doc_context(req.attached_documents)
        doc_type = req.doc_type if req.doc_type in DOC_TYPES else _classify_doc_type(req.prompt)
        user_memory = build_context(db, user_id)
        plan = run_planner(
            req.prompt,
            [],
            settings.planner_model,
            user_memory=user_memory,
            doc_context=doc_context,
        )
        use_deep_research = bool(req.deep_research)
        use_web = (not use_deep_research) and bool(req.web_search)
        research_cost = 0.0
        research_latency_ms = 0
        if use_deep_research:
            if not is_admin:
                check_rate_limit(f"research:{user_id}", settings.rate_limit_research_per_hour, 3600)
            research = run_research(
                db,
                user_id=user_id,
                conversation_id=None,
                query=plan.enriched_prompt or req.prompt,
                profile=req.profile,
                force_model=req.force_model,
                mode=req.research_mode if req.research_mode != "quick" else "deep",
                progress=lambda *_args, **_kwargs: None,
            )
            research_cost = research.result.estimated_cost_usd or 0.0
            research_latency_ms = research.result.latency_ms
            research_context = (
                "SOURCE-GROUNDED RESEARCH SYNTHESIS FOR THIS DOCUMENT:\n\n"
                f"{research.result.answer}"
            )
            doc_context = "\n\n".join(part for part in [doc_context, research_context] if part)

        web_context = gather_web_context(plan.search_query or req.prompt, use_web)
        route = choose_route(
            req.prompt,
            req.profile,
            req.force_model,
            task_override="writing",
            complexity_override="high",
            web_search=use_web,
        )
        if plan.planner_model != "none":
            route.reason = f"[planner:{plan.planner_model} {plan.planner_latency_ms}ms] {route.reason}"
        if use_web:
            route.reason = f"{route.reason} {web_context.status}"
        if use_deep_research:
            route.reason = f"{route.reason} Deep research synthesis used as source material."
        result = invoke_llm(
            plan.enriched_prompt or req.prompt,
            route,
            deep_research=False,
            web_context=web_context.context,
            enable_native_search=use_web,
            planner_context=plan.context_summary or None,
            doc_context=doc_context or None,
            artifact_context=_document_system_prompt(doc_type, req),
        )
        result.estimated_cost_usd = (
            (result.estimated_cost_usd or 0.0)
            + plan.planner_cost_usd
            + research_cost
        )
        result.latency_ms = result.latency_ms + plan.planner_latency_ms + research_latency_ms
        title = req.title or _title_from_markdown(result.answer) or "Fronei document"
        content = generate_docx_bytes(title, result.answer, "Generated by Fronei", doc_type=doc_type)
        db.add(RequestLog(
            user_id=user_id,
            message=req.prompt,
            task_type="document_generation",
            complexity=route.complexity,
            profile=route.profile,
            selected_model=route.primary_model,
            model_used=result.model_used,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            status="success",
        ))
        db.commit()
        return DocumentGenerateFromPromptResponse(
            title=title,
            doc_type=doc_type,
            markdown=result.answer,
            filename=f"{_safe_filename(title)}.docx",
            docx_base64=base64.b64encode(content).decode("ascii"),
            model_used=result.model_used,
            estimated_cost_usd=result.estimated_cost_usd,
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.add(RequestLog(
            user_id=user_id,
            message=req.prompt,
            task_type="document_generation",
            complexity=route.complexity if route else "high",
            profile=route.profile if route else (req.profile or settings.default_profile),
            selected_model=route.primary_model if route else "none",
            model_used="none",
            latency_ms=0,
            status="error",
            error=str(exc),
        ))
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()


def _safe_filename(title: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in title.strip())
    safe = "-".join(part for part in safe.split("-") if part)
    return (safe or "fronei-document")[:80]


def _title_from_markdown(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _classify_doc_type(prompt: str) -> str:
    text = prompt.lower()
    if any(term in text for term in ["meeting notes", "minutes", "meeting recap", "attendees", "agenda"]):
        return "meeting_notes"
    if any(term in text for term in ["technical spec", "technical specification", "implementation spec", "architecture spec", "requirements document"]):
        return "technical_spec"
    if any(term in text for term in ["proposal", "propose", "statement of work", "sow", "commercial offer"]):
        return "proposal"
    if any(term in text for term in ["one pager", "one-pager", "1 pager", "single page", "single-page"]):
        return "one_pager"
    if any(term in text for term in ["memo", "memorandum", "internal note"]):
        return "memo"
    if any(term in text for term in ["letter", "cover letter", "formal letter"]):
        return "letter"
    if any(term in text for term in ["executive report", "client report", "board report", "report", "assessment"]):
        return "executive_report"
    return "executive_report"


def _document_system_prompt(
    doc_type: str,
    req: DocumentGenerateFromPromptRequest | None = None,
) -> str:
    parts = [DOCUMENT_SYSTEM_PROMPT, DOC_TYPE_PROMPTS.get(doc_type, DOC_TYPE_PROMPTS["executive_report"])]
    if req:
        preferences = []
        if req.audience:
            preferences.append(f"- Audience: {req.audience}")
        if req.tone:
            preferences.append(f"- Tone: {req.tone}")
        if req.length:
            preferences.append(f"- Length/depth: {req.length}")
        if req.output_formats:
            preferences.append(f"- Requested output formats: {', '.join(req.output_formats)}")
        if preferences:
            parts.append(
                "User-selected document brief:\n"
                + "\n".join(preferences)
                + "\nHonor these preferences unless they directly conflict with the user's source material."
            )
    return "\n\n".join(parts)


# Strong, unambiguous signals that the user wants a specific document type.
DOC_INTENT_KEYWORDS: dict[str, list[str]] = {
    "resume": ["resume", "résumé", " cv ", "curriculum vitae"],
    "letter": ["cover letter", "recommendation letter", "formal letter", "letter to"],
    "memo": ["memo", "memorandum"],
    "proposal": ["proposal", "statement of work", " sow ", "sow document"],
    "meeting_notes": ["meeting notes", "meeting minutes", "minutes of the meeting", "meeting recap"],
    "technical_spec": ["technical spec", "technical specification", "architecture spec", "design doc", "requirements document"],
    "one_pager": ["one pager", "one-pager", "1-pager", "1 pager"],
    "executive_report": ["executive report", "board report", "status report", "client report"],
}

# Action verbs + generic document nouns also signal intent (e.g. "write a report on X").
DOC_ACTION_VERBS = ["write", "draft", "create", "generate", "prepare", "produce", "build", "put together"]
DOC_GENERIC_TERMS = ["document", " doc ", "report", "write-up", "writeup", "word file", "word doc", ".docx", "downloadable"]


def detect_document_intent(prompt: str) -> str | None:
    """Return a doc_type if the prompt clearly asks for a generated document, else None.

    This is intentionally conservative — it should only fire on requests that are
    plainly asking for a formatted, downloadable deliverable (resume, memo, proposal,
    report, etc.), not on ordinary chat questions.
    """
    text = f" {prompt.lower()} "
    for doc_type, keywords in DOC_INTENT_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return doc_type
    if any(verb in text for verb in DOC_ACTION_VERBS) and any(term in text for term in DOC_GENERIC_TERMS):
        return "executive_report"
    return None


def build_document_artifact(title: str, body_markdown: str, doc_type: str, fmt: str = "markdown") -> dict:
    """Build a document_preview payload for a planner-driven document output.

    Supports the phase-1 formats (markdown, docx). Any other requested format
    falls back to markdown (the picker keeps unsupported formats disabled).
    """
    title = title or _title_from_markdown(body_markdown) or "Fronei document"
    preview: dict = {
        "title": title,
        "doc_type": doc_type,
        "format": "markdown",
        "markdown": body_markdown,
        "filename": f"{_safe_filename(title)}.md",
    }
    if fmt == "docx":
        try:
            content = generate_docx_bytes(title, body_markdown, "Generated by Fronei", doc_type=doc_type)
            preview["format"] = "docx"
            preview["filename"] = f"{_safe_filename(title)}.docx"
            preview["docx_base64"] = base64.b64encode(content).decode("ascii")
        except Exception:
            pass
    elif fmt == "xlsx":
        try:
            content = generate_xlsx_bytes(title, body_markdown, "Generated by Fronei", doc_type=doc_type)
            preview["format"] = "xlsx"
            preview["filename"] = f"{_safe_filename(title)}.xlsx"
            preview["xlsx_base64"] = base64.b64encode(content).decode("ascii")
        except Exception:
            pass
    return preview


def build_document_preview(prompt: str, answer: str) -> dict | None:
    """If `prompt` looks like a document-generation request, render `answer` as a
    DOCX and return a preview payload suitable for the SSE `done` event.
    Returns None if the prompt doesn't look like a document request.
    """
    doc_type = detect_document_intent(prompt)
    if not doc_type:
        return None
    title = _title_from_markdown(answer) or "Fronei document"
    try:
        content = generate_docx_bytes(title, answer, "Generated by Fronei", doc_type=doc_type)
    except Exception:
        return None
    return {
        "title": title,
        "doc_type": doc_type,
        "markdown": answer,
        "filename": f"{_safe_filename(title)}.docx",
        "docx_base64": base64.b64encode(content).decode("ascii"),
    }
