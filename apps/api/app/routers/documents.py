import base64
import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
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
from app.services.document_generator import (
    compose_deck_plan_parallel,
    deck_plan_to_markdown,
    generate_docx_bytes,
    generate_pptx_bytes,
    generate_xlsx_bytes,
    parse_deck_plan,
    repair_deck_plan_for_qa,
)
from app.services.document_templates import (
    archive_user_template,
    list_document_templates,
    store_user_pptx_template,
)
from app.services.pptx_render_qa import run_pptx_render_qa
from app.services.llm_gateway import invoke_llm
from app.services.personal_context import build_context
from app.services.planner import run_planner
from app.services.rate_limit import check_rate_limit, rate_limiter
from app.services.research_orchestrator import run_research
from app.services.router import choose_route
from app.services.web_context import gather_web_context

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 30 * 1024 * 1024   # 30 MB
SUPPORTED_RENDER_FORMATS = {"markdown", "docx", "xlsx", "pptx"}
DOC_TYPES = {
    "executive_report",
    "proposal",
    "memo",
    "technical_spec",
    "meeting_notes",
    "one_pager",
    "letter",
    "resume",
    "presentation",
}
DOCUMENT_SYSTEM_PROMPT = """You are Fronei's document generation engine, writing for mid-level cross-functional \
professionals (strategy, architecture, design, engineering) who must deliver polished artifacts to senior \
internal and external stakeholders. The bar is "I could paste this into a deliverable to my VP or client today \
with minimal edits" — not "this reads like an AI wrote it in 30 seconds."

Output rules:
- Output only the document body in the format required by the selected document type. Do not include commentary \
about generating the document.
- For Markdown-based document types, start with a strong H1 title unless the user explicitly asks for a different format.
- For Markdown-based document types, use Markdown headings, tables for comparative or numeric data, and bold for key terms.
- For Markdown-based document types, keep the document coherent enough to paste directly into a Word document.
- Do not invent precise facts, metrics, dates, legal claims, or citations not supplied by the user.
- If source-grounded research or web context is provided, use it as source material and preserve useful citations.

Substance rules — this is what separates this from generic AI output:
- Every section must earn its place. If a section would just restate the prompt or fill space, cut it or merge \
it into something with actual content.
- Lead with the "so what." Each major section should open with the conclusion or implication, then support it —
  not build up to a vague point at the end.
- Be specific, not generic. Replace placeholders like "various stakeholders," "robust solution," "in today's \
fast-paced environment," "leverage synergies," "best-in-class," "holistic approach," "moving forward," and similar \
filler with concrete nouns, numbers, names, or mechanisms drawn from the user's input. If specifics aren't \
available, say what's missing rather than papering over it with vague language.
- Avoid repetition across sections — do not restate the same point in the summary, body, and conclusion in \
slightly different words. Each section should add new information.
- Where the request involves a decision, trade-off, or comparison, structure it as a true comparison (table or \
explicit criteria), not a list of pros that all sound positive.
- Make assumptions explicit in a short, visually distinct "Assumptions" note near the top — don't bury them or \
silently invent context.
- Match vocabulary and depth to the stated audience: technical depth and precise terminology for engineering/\
architecture audiences; outcomes, cost, and risk framing for executive or external audiences; avoid jargon the \
audience wouldn't use themselves.
"""
DOC_TYPE_PROMPTS = {
    "executive_report": """Document type: executive_report
Expected structure:
- H1 title
- Executive summary (3-5 sentences: the situation, the recommendation, and the expected impact — readable on \
its own with no other context)
- Situation / background
- Analysis, using tables where the information is data-heavy or comparative; each analytical point should state \
its implication, not just the observation
- Recommendations, each tied to a concrete rationale and expected outcome
- Risks and mitigations — name specific risks relevant to this situation, not generic categories
- Next steps, with owners/timing if the user supplied or implied them
Use concise, decision-oriented language suitable for clients or senior stakeholders. A senior reader should be \
able to act on this from the executive summary alone.""",
    "proposal": """Document type: proposal
Expected structure:
- H1 title
- Problem statement — framed in terms of cost, risk, or missed opportunity to the reader, not just a description
- Proposed approach — the actual mechanism of how this solves the problem, not just "we will do X"
- Scope and timeline
- Cost / ROI, using tables where helpful; tie cost to the value/outcome described above
- Terms, assumptions, or dependencies
- Next steps
Keep the tone confident, practical, and commercially credible — avoid sales-brochure language ("game-changing," \
"revolutionary," "unparalleled").""",
    "memo": """Document type: memo
Expected structure:
- H1 title
- Header block with To, From, Date, and Re
- Purpose — one or two sentences, stated directly
- Body — get to the point in the first paragraph; supporting detail follows
- Action items, each with a clear owner/action, not just topics
Keep it concise, direct, and easy to skim. A memo that takes more than 60 seconds to find the point has failed.""",
    "technical_spec": """Document type: technical_spec
Expected structure:
- H1 title
- Overview — what is being built/changed and why, in one paragraph
- Architecture — concrete components, data flow, and interfaces; use a diagram description or table if it \
clarifies structure
- Requirements — functional and non-functional, stated as testable statements
- Implementation notes — specific decisions and the reasoning behind them, including alternatives considered \
where relevant
- Risks / constraints — name the actual technical risks (e.g., specific failure modes, scaling limits, \
dependencies), not generic "there are risks"
- Open questions — genuinely unresolved items, not rhetorical
Use precise technical language and tables for requirements, interfaces, or trade-offs. Write for an engineering \
audience that will use this to make implementation decisions.""",
    "meeting_notes": """Document type: meeting_notes
Expected structure:
- H1 title
- Attendees
- Agenda
- Discussion summary — capture the actual reasoning and disagreements, not just topic labels
- Decisions — stated as decisions, with the rationale if discussed
- Action items with owners and due dates when available
Do not invent attendees, owners, or dates that were not provided.""",
    "one_pager": """Document type: one_pager
Expected structure:
- H1 headline that states the point, not just the topic
- 3-5 key points, each substantive enough to stand alone
- Supporting facts or rationale
- Single call-to-action
Keep it tight enough to fit on one page. Every sentence should be load-bearing.""",
    "letter": """Document type: letter
Expected structure:
- Date
- Recipient / salutation when provided
- Opening purpose — state why you're writing in the first sentence
- Body
- Closing and signature placeholder
Use polished, professional letter language without stock phrases ("I hope this finds you well," "please don't \
hesitate to reach out") unless the user's tone preference calls for them.""",
    "presentation": """Document type: presentation
Write the document body as valid DeckPlan JSON only. Do not output Markdown for the body.

DeckPlan schema:
{
  "title": "Deck title",
  "subtitle": "Audience, client, or context",
  "slides": [
    {
      "layout": "section | cover | bullets | executive_summary | comparison | architecture | table | \
recommendation | timeline | risk_matrix | financial_model | stat_cards | appendix | takeaways",
      "density": "low | medium | high (optional — your estimate of how much content this slide carries; \
the renderer also computes this and will trim automatically if you under-call it)",
      "title": "Short assertion-style slide title (target 40-60 chars, hard cap ~80)",
      "bullets": ["short support point"],
      "columns": [
        {"heading": "Option A", "bullets": ["short point"]},
        {"heading": "Option B", "bullets": ["short point"]}
      ],
      "table": {
        "headers": ["Criterion", "Option A", "Option B"],
        "rows": [["Cost", "Low", "Medium"]]
      },
      "phases": [
        {"label": "Phase 1 / Q1", "title": "Foundation", "description": "What happens in this phase"}
      ],
      "chart": {
        "type": "bar | line | pie",
        "categories": ["2024", "2025", "2026"],
        "series": [{"name": "Revenue", "values": [1.2, 1.8, 2.6]}]
      },
      "stats": [
        {"value": "$4.2M", "label": "Annual run-rate savings", "source": "optional citation"}
      ],
      "callout": {"label": "Key Insight", "text": "One or two sentences elaborating on the stats above."},
      "speaker_notes": "Presenter talk track, nuance, caveats, and transitions."
    }
  ]
}

Layout guide (use the most specific layout that fits — generic `bullets` is an exception, not the default):
- Before writing slides, choose the deck's story spine and assign each slide a visual job. A good strategy deck \
is not a sequence of text boxes; it is a sequence of decisions, proof objects, comparisons, diagrams, timelines, \
and takeaways.
- `cover`: optional opening slide for the deck's title/positioning statement when it needs more presence than \
the default title slide (renders like `section`).
- `executive_summary`: first content slide for executive_report/proposal decks. `bullets[0]` is the single \
"so what" headline (one sentence, the bottom line); remaining bullets are supporting points.
- `recommendation`: use for the decision/ask slide. `bullets[0]` is the recommendation itself (rendered in an \
accent callout); remaining bullets are the rationale.
- `timeline`: phased plans, migration paths, operating models, and roadmaps. Provide `phases` (3-6 entries, each \
with `label` e.g. "Phase 1" or a date/quarter, `title`, and a short `description`). Falls back to `bullets` as \
phase titles if `phases` is omitted.
- `architecture`: technical/system-design slides. Either provide `columns` (diagram side vs. explanation side) \
or `bullets` describing components/data flow — a diagram placeholder is rendered alongside.
- `comparison`: use `columns` with 2-3 concise cards for trade-offs, operating-model, ownership, governance, \
capability, or design-principle slides.
- `risk_matrix`: provide a `table` with headers `["Risk", "Likelihood", "Impact", "Owner"]` (or similar risk \
register columns).
- `financial_model`: provide a `chart` (preferred — renders as a native chart) and/or a `table` of figures \
(e.g. cost/benefit, ROI, budget by line item, revenue projection). `chart.categories` are the x-axis labels \
(e.g. years/quarters) and each `series` entry is a numeric line/bar with a name. Use `type: "line"` for trends \
over time, `"bar"` for comparisons across categories, `"pie"` for composition/share. All `series.values` must be \
plain numbers (no currency symbols, commas, or percent signs).
- `stat_cards`: market-context / "by the numbers" slides. Provide up to 4 `stats` entries, each with a short \
`value` (e.g. "$4.2M", "37%", "3.5x" — keep to ~16 chars) and a `label` describing what it measures. Optionally \
add a `callout` (`label` + `text`) below the cards to interpret what the numbers mean for the stakeholder — this \
is the highest-impact slide for grounding a deck in concrete numbers, so use it whenever the source material \
contains 2-4 strong metrics.
- `bullets`: use only when no stronger visual archetype fits. Even then, write the first bullet as the core \
insight and the next 2-3 bullets as short proof points; the renderer will turn these into an insight panel plus \
supporting cards, not a traditional bullet list.
- `takeaways`: use for the closing synthesis. It should not repeat the executive summary; it should tell the \
stakeholder what to remember and what happens next.
- Any slide may include a `chart` alongside or instead of a `table` when the underlying data is genuinely \
numeric and a chart communicates the point better than a table.
- `appendix`: reference/backup material placed at the end of the deck after the main narrative. Denser bullet \
lists (up to ~10) are acceptable here.
- `table`: genuine structured comparisons or numeric data not covered by risk_matrix/financial_model.
- `section`: sparingly, to separate major parts of the story.

Deck quality rules:
- Build a narrative arc: context -> analysis/options -> recommendation -> next steps. For executive_report and \
proposal decks, open the content with an `executive_summary` slide and close with a `recommendation` slide.
- Use 6-12 slides unless the user asks for a shorter or longer deck.
- Slide titles must make a point on their own, not label a topic — write a short assertion, not a run-on \
sentence. Target 40-60 characters and never exceed ~80; titles must fit on one line. Use "Strangler migration \
cuts delivery risk" instead of "Migration options" or "An analysis of how a strangler-pattern migration \
approach can help reduce overall delivery risk for the platform." If a title runs long, cut it down to its core \
claim rather than relying on the renderer to truncate it.
- Bullets must be short, specific, and scannable. Prefer 2-3 bullets per slide, and no more than 6 — extra \
bullets are automatically dropped from the slide (and moved to speaker_notes) rather than rendered, so don't \
pad a slide expecting all of it to be visible.
- Each bullet should fit on one line (~90 characters). Longer bullets are truncated on the slide and the full \
text is moved to speaker_notes — write the slide-visible portion as the complete thought, with elaboration in \
speaker_notes instead of a longer bullet.
- Every non-section slide must have a proof object. Choose one: `stats`, `chart`, `table`, `columns`, `phases`, \
or a tightly written `bullets` insight panel. Do not emit a slide whose only job is "more text about the topic."
- Prefer object-rich decks over sparse outlines: a board-quality 10-slide deck should usually include at least \
one stat-card slide, one structured comparison/cards slide, one roadmap/timeline, one recommendation/decision \
slide, and one technical or operating-model diagram when the topic supports it.
- Use speaker_notes to carry nuance, assumptions, data caveats, and the talk track that should not clutter slides. \
Treat slide copy and speaker_notes as separate channels: slide copy is the headline, speaker_notes is everything \
else.
- Do not invent precise facts, figures, names, dates, or citations not supplied by the user or source context.
- Avoid generic consulting filler. Every slide should answer: "what should the stakeholder understand, decide, \
or do?"
- If a template/design brief is provided, treat it as binding: choose layouts and text density that fit that \
template instead of forcing the template to absorb an article.

After the DeckPlan JSON body, append the required `---SUMMARY---` section as instructed separately. The summary \
may be Markdown bullets, but the body before `---SUMMARY---` must remain valid JSON.""",
    "resume": """Document type: resume
Expected structure:
- H1 with the person's name
- Contact line (location, email, phone, LinkedIn if provided)
- Professional summary (2-4 sentences)
- Work experience, most recent first, each with company, title, location, dates, and bullet achievements
- Skills, grouped by category
- Certifications (if provided)
- Education
Use concise, achievement-oriented bullets (action verb + quantified result). Do not invent \
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


@router.get("/templates")
def list_templates(
    doc_type: str = "presentation",
    user_id: str = CurrentUser,
) -> dict:
    db = SessionLocal()
    try:
        return {"templates": list_document_templates(doc_type, db=db, user_id=user_id)}
    finally:
        db.close()


@router.post("/templates")
async def upload_template(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
    user_id: str = CurrentUser,
) -> dict:
    content = await file.read()
    db = SessionLocal()
    try:
        try:
            row = store_user_pptx_template(
                db,
                user_id,
                filename=file.filename or "template.pptx",
                content_type=file.content_type,
                data=content,
                name=name,
                description=description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description or f"Uploaded from {row.original_filename or 'PowerPoint template'}",
            "recommended": True,
            "user_template": True,
        }
    finally:
        db.close()


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: str,
    user_id: str = CurrentUser,
) -> dict:
    db = SessionLocal()
    try:
        if not archive_user_template(db, user_id, template_id):
            raise HTTPException(status_code=404, detail="Template not found")
        return {"status": "ok"}
    finally:
        db.close()


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
    "presentation": ["presentation", "slide deck", "slides", "ppt", "pptx", "powerpoint", "board deck", "pitch deck"],
}

# Action verbs + generic document nouns also signal intent (e.g. "write a report on X").
DOC_ACTION_VERBS = ["write", "draft", "create", "generate", "prepare", "produce", "build", "put together"]
DOC_GENERIC_TERMS = [
    "document", " doc ", "report", "write-up", "writeup", "word file", "word doc", ".docx", "downloadable",
    "presentation", "slide deck", "slides", "ppt", "pptx", "powerpoint",
]


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


def build_document_artifact(
    title: str,
    body_markdown: str,
    doc_type: str,
    fmt: str = "markdown",
    template_id: str | None = None,
    template_path: str | Path | None = None,
) -> dict:
    """Build a document_preview payload for a planner-driven document output.

    Unsupported or failed binary renders fall back to Markdown, but the payload
    carries an explicit generation_error so the UI can be honest with users.
    """
    deck_plan = parse_deck_plan(body_markdown) if doc_type == "presentation" else None
    composition: dict | None = None
    if deck_plan:
        deck_plan, composition = compose_deck_plan_parallel(deck_plan)
        body_markdown = json.dumps(deck_plan)
    title = (deck_plan or {}).get("title") or title or _title_from_markdown(body_markdown) or "Fronei document"
    requested_format = fmt if fmt in SUPPORTED_RENDER_FORMATS else "markdown"
    if fmt in SUPPORTED_RENDER_FORMATS and doc_type == "presentation" and requested_format == "markdown":
        requested_format = "pptx"
    display_markdown = deck_plan_to_markdown(json.dumps(deck_plan)) if deck_plan else None
    preview: dict = {
        "title": title,
        "doc_type": doc_type,
        "format": "markdown",
        "requested_format": requested_format if fmt in SUPPORTED_RENDER_FORMATS else fmt,
        "markdown": display_markdown or body_markdown,
        "filename": f"{_safe_filename(title)}.md",
    }
    if composition:
        preview["composition"] = {k: v for k, v in composition.items() if k != "jobs"}
    if fmt not in SUPPORTED_RENDER_FORMATS:
        preview["generation_error"] = f"{fmt} output is not supported yet; showing Markdown instead."
        return preview
    if requested_format == "markdown":
        return preview
    if requested_format == "docx":
        try:
            content = generate_docx_bytes(title, body_markdown, "Generated by Fronei", doc_type=doc_type)
            preview["format"] = "docx"
            preview["filename"] = f"{_safe_filename(title)}.docx"
            preview["docx_base64"] = base64.b64encode(content).decode("ascii")
        except Exception as exc:
            logger.exception("Failed to render DOCX artifact")
            preview["generation_error"] = f"Word rendering failed: {exc}"
    elif requested_format == "xlsx":
        try:
            content = generate_xlsx_bytes(title, body_markdown, "Generated by Fronei", doc_type=doc_type)
            preview["format"] = "xlsx"
            preview["filename"] = f"{_safe_filename(title)}.xlsx"
            preview["xlsx_base64"] = base64.b64encode(content).decode("ascii")
        except Exception as exc:
            logger.exception("Failed to render XLSX artifact")
            preview["generation_error"] = f"Excel rendering failed: {exc}"
    elif requested_format == "pptx":
        try:
            content = generate_pptx_bytes(
                title,
                body_markdown,
                "Generated by Fronei",
                template_id=template_id,
                template_path=template_path,
            )
            render_qa: dict | None = None
            if get_settings().pptx_render_qa_enabled:
                try:
                    render_qa = run_pptx_render_qa(content)
                except Exception:
                    logger.exception("PPTX render QA failed")

            # Repair loop: if render QA flags crowded slides and we have a
            # structured DeckPlan, apply small deterministic edits (drop a
            # bullet/row/phase) and re-render, re-checking each time. Stops
            # as soon as issues clear, no further repair is possible, or a
            # small iteration cap is hit.
            repair_iterations = 0
            if (
                deck_plan
                and render_qa
                and render_qa.get("available")
                and any(i.get("type") in {"dense_text", "dense_ink", "tiny_text_risk"} for i in render_qa.get("issues") or [])
            ):
                current_plan = deck_plan
                for _ in range(2):
                    repaired_plan, changed = repair_deck_plan_for_qa(current_plan, render_qa["issues"])
                    if not changed:
                        break
                    try:
                        repaired_content = generate_pptx_bytes(
                            title,
                            json.dumps(repaired_plan),
                            "Generated by Fronei",
                            template_id=template_id,
                            template_path=template_path,
                        )
                        repaired_qa = run_pptx_render_qa(repaired_content)
                    except Exception:
                        logger.exception("PPTX repair-loop re-render failed")
                        break
                    repair_iterations += 1
                    current_plan = repaired_plan
                    content = repaired_content
                    render_qa = repaired_qa
                    if not any(
                        i.get("type") in {"dense_text", "dense_ink", "tiny_text_risk"}
                        for i in (repaired_qa.get("issues") or [])
                    ):
                        break
                if repair_iterations:
                    display_markdown_repaired = deck_plan_to_markdown(json.dumps(current_plan))
                    if display_markdown_repaired:
                        preview["markdown"] = display_markdown_repaired
                    render_qa["repair_iterations"] = repair_iterations

            preview["format"] = "pptx"
            preview["filename"] = f"{_safe_filename(title)}.pptx"
            preview["pptx_base64"] = base64.b64encode(content).decode("ascii")
            if render_qa is not None:
                preview["render_qa"] = render_qa
        except Exception as exc:
            logger.exception("Failed to render PPTX artifact")
            preview["generation_error"] = f"PowerPoint rendering failed: {exc}"
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
    if doc_type == "presentation":
        return build_document_artifact(title, answer, doc_type, "pptx")
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
