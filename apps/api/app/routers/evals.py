"""Admin eval endpoints.

Eval case CRUD:
  GET    /admin/evals/cases                   List active cases (?include_inactive=true for all)
  POST   /admin/evals/cases                   Create case
  GET    /admin/evals/cases/{id}              Get case
  PUT    /admin/evals/cases/{id}              Update case
  DELETE /admin/evals/cases/{id}              Soft-delete (sets is_active=False)
  GET    /admin/evals/cases/{id}/history      Per-case run history across all runs
  POST   /admin/evals/cases/{id}/restore      Reactivate a soft-deleted case
  POST   /admin/evals/cases/upload            Bulk upsert from JSON array

General eval runs (LangGraph, structural + criteria scoring):
  POST   /admin/evals/runs                    Start a run over selected (or all) cases
  GET    /admin/evals/runs                    List recent runs
  GET    /admin/evals/runs/{run_id}/status    Poll for live progress + final results
  GET    /admin/evals/runs/{run_id}/result    Full results (alias; checks memory then DB)
  POST   /admin/evals/runs/{run_id}/stop      Request early termination of a running eval
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Response
from fastapi.responses import StreamingResponse  # still used by eval-runs stream
from pydantic import BaseModel, Field

from app.auth import AdminPrincipal, RequireAdmin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/evals", tags=["admin"])

@router.get("/langsmith/status")
def get_langsmith_status(admin: AdminPrincipal = RequireAdmin) -> dict:
    """Return LangSmith configuration status."""
    from app.services.langsmith_evals import is_configured
    from app.config import get_settings
    s = get_settings()
    configured = is_configured()
    return {
        "configured": configured,
        "project": s.langchain_project if configured else None,
        "tracing_on": os.environ.get("LANGCHAIN_TRACING_V2") == "true" if configured else False,
        "dataset_name": "fronei-eval-cases",
    }


# ===========================================================================
# Eval case CRUD
# ===========================================================================

# Consolidated category set (see alembic d4e5f6a7b8c9 for the migration that
# rewrote pre-existing case rows into these buckets). Cases may still carry a
# category outside this list (e.g. freeform values from /cases/upload), but
# the admin UI groups by these first.
EVAL_CASE_CATEGORIES = [
    "routing_classification",
    "freshness_facts",
    "subject_extraction",
    "evidence_quality",
    "domain_specific",
    "answer_behavior",
]


EVAL_CASE_ROUTES = ["direct", "clarify", "research", "document", "research_document"]


class EvalCaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1)
    category: str | None = Field(default=None, max_length=128)
    # List of natural-language criteria strings, e.g. ["mentions official SLA", "cites practitioner data"]
    expected_criteria: list[str] = Field(default_factory=list)
    expected_primary_role: str | None = Field(default=None, max_length=64)
    min_independent_sources: int | None = Field(default=None, ge=0)
    # Structured benchmark thresholds — scored deterministically, not by the LLM judge.
    min_evidence_items: int | None = Field(default=None, ge=0)
    min_criteria_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # Which orchestrator route this query SHOULD resolve to. Null = don't
    # assert on routing, just grade whatever route the orchestrator picks.
    expected_route: str | None = Field(default=None, max_length=32)
    # v2 scoring schema's optional nested sections (routing.expected_gate_fires/
    # expected_gate_silent, retrieval_requirements, cost_latency_budget, etc.)
    # — see eval_case_schema.json case_template. Permissive dict, not a strict
    # nested model: the schema has open implementation questions and is still
    # evolving (scoring_spec.md §4); validating loosely here keeps the API from
    # needing a migration every time a sub-field gets refined.
    v2_spec: dict[str, Any] | None = None
    notes: str | None = None


class EvalCaseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    query: str | None = Field(default=None, min_length=1)
    category: str | None = None
    expected_criteria: list[str] | None = None
    expected_primary_role: str | None = None
    min_independent_sources: int | None = Field(default=None, ge=0)
    min_evidence_items: int | None = Field(default=None, ge=0)
    min_criteria_score: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_route: str | None = Field(default=None, max_length=32)
    v2_spec: dict[str, Any] | None = None
    notes: str | None = None


def _case_out(c) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "query": c.query,
        "category": c.category,
        "expected_criteria": json.loads(c.expected_criteria_json or "[]"),
        "expected_primary_role": c.expected_primary_role,
        "min_independent_sources": c.min_independent_sources,
        "min_evidence_items": getattr(c, "min_evidence_items", None),
        "min_criteria_score": getattr(c, "min_criteria_score", None),
        "expected_route": getattr(c, "expected_route", None),
        "v2_spec": json.loads(c.v2_spec_json) if getattr(c, "v2_spec_json", None) else None,
        "notes": c.notes,
        "is_active": c.is_active,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


@router.get("/cases")
def list_eval_cases(
    include_inactive: bool = False,
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        q = db.query(EvalCase)
        if not include_inactive:
            q = q.filter(EvalCase.is_active.is_(True))
        cases = q.order_by(EvalCase.created_at.desc()).all()
        return {"items": [_case_out(c) for c in cases], "total": len(cases)}
    finally:
        db.close()


@router.post("/cases", status_code=201)
def create_eval_case(body: EvalCaseCreate, admin: AdminPrincipal = RequireAdmin) -> dict:
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        case = EvalCase(
            title=body.title,
            query=body.query,
            category=body.category,
            expected_criteria_json=json.dumps(body.expected_criteria),
            expected_primary_role=body.expected_primary_role,
            min_independent_sources=body.min_independent_sources,
            min_evidence_items=body.min_evidence_items,
            min_criteria_score=body.min_criteria_score,
            expected_route=body.expected_route,
            v2_spec_json=json.dumps(body.v2_spec) if body.v2_spec else None,
            notes=body.notes,
            created_by=admin.user_id,
            created_at=now,
            updated_at=now,
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        return _case_out(case)
    finally:
        db.close()


@router.get("/cases/{case_id}")
def get_eval_case(case_id: int, admin: AdminPrincipal = RequireAdmin) -> dict:
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        case = db.query(EvalCase).filter(EvalCase.id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail=f"Eval case {case_id} not found.")
        return _case_out(case)
    finally:
        db.close()


@router.put("/cases/{case_id}")
def update_eval_case(case_id: int, body: EvalCaseUpdate, admin: AdminPrincipal = RequireAdmin) -> dict:
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        case = db.query(EvalCase).filter(EvalCase.id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail=f"Eval case {case_id} not found.")
        if body.title is not None:
            case.title = body.title
        if body.query is not None:
            case.query = body.query
        if body.category is not None:
            case.category = body.category
        if body.expected_criteria is not None:
            case.expected_criteria_json = json.dumps(body.expected_criteria)
        if body.expected_primary_role is not None:
            case.expected_primary_role = body.expected_primary_role
        if body.min_independent_sources is not None:
            case.min_independent_sources = body.min_independent_sources
        if body.min_evidence_items is not None:
            case.min_evidence_items = body.min_evidence_items
        if body.min_criteria_score is not None:
            case.min_criteria_score = body.min_criteria_score
        if body.expected_route is not None:
            case.expected_route = body.expected_route
        if body.v2_spec is not None:
            case.v2_spec_json = json.dumps(body.v2_spec)
        if body.notes is not None:
            case.notes = body.notes
        case.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(case)
        return _case_out(case)
    finally:
        db.close()


@router.delete("/cases/{case_id}", status_code=204)
def delete_eval_case(case_id: int, admin: AdminPrincipal = RequireAdmin) -> None:
    """Soft-delete: sets is_active=False. Case is hidden from normal queries but never erased."""
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        case = db.query(EvalCase).filter(EvalCase.id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail=f"Eval case {case_id} not found.")
        case.is_active = False
        case.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


@router.get("/cases/{case_id}/history")
def get_case_run_history(case_id: int, limit: int = 20, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Return past eval run results for a specific case (newest first).

    Scans recent EvalRun rows for those that included case_id, extracts the
    per-case result from results_json, and returns a compact history list.
    Max scanned rows: 50; max returned entries: limit (default 20).
    """
    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(EvalRun).order_by(EvalRun.started_at.desc()).limit(50).all()
        history = []
        for row in rows:
            # Fast pre-filter: check case_ids_json before parsing results
            case_ids = json.loads(row.case_ids_json or "[]")
            if case_id not in case_ids:
                continue
            raw = json.loads(row.results_json or "null")
            if not raw:
                continue
            # Normalise envelope vs. raw list
            if isinstance(raw, list):
                results_list = raw
            elif isinstance(raw, dict):
                results_list = raw.get("cases", [])
            else:
                continue
            case_result = next((r for r in results_list if r.get("case_id") == case_id), None)
            if not case_result:
                continue

            def _entry_for(run: dict) -> dict:
                crit = run.get("criteria") or {}
                return {
                    "ok": run.get("ok"),
                    "answer_length": run.get("answer_length"),
                    "evidence_count": run.get("evidence_count"),
                    "claim_count": run.get("claim_count"),
                    "latency_ms": run.get("latency_ms"),
                    "criteria_score": crit.get("score"),
                    "criteria_passed": crit.get("passed") or [],
                    "criteria_failed": crit.get("failed") or [],
                    "answer": run.get("answer", "")[:500],
                }

            if "pipeline" in case_result and "run" in case_result:
                # New single-pipeline shape.
                history.append({
                    "run_id": row.id,
                    "status": row.status,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "pipeline": case_result.get("pipeline"),
                    "overall_structural_pass": case_result.get("overall_structural_pass"),
                    "result": _entry_for(case_result.get("run") or {}),
                })
            else:
                # Legacy dual-pipeline shape from runs predating the single-pipeline
                # eval redesign — keep rendering old history entries as-is.
                history.append({
                    "run_id": row.id,
                    "status": row.status,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "pipeline": "both",
                    "overall_structural_pass": case_result.get("overall_structural_pass"),
                    "legacy": _entry_for(case_result.get("legacy") or {}),
                    "langgraph": _entry_for(case_result.get("langgraph") or {}),
                })
            if len(history) >= limit:
                break
        return {"case_id": case_id, "history": history}
    finally:
        db.close()


@router.post("/cases/{case_id}/restore", status_code=200)
def restore_eval_case(case_id: int, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Reactivate a soft-deleted case."""
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        case = db.query(EvalCase).filter(EvalCase.id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail=f"Eval case {case_id} not found.")
        case.is_active = True
        case.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(case)
        return _case_out(case)
    finally:
        db.close()


class EvalCaseUploadItem(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1)
    category: str | None = None
    expected_criteria: list[str] = Field(default_factory=list)
    expected_primary_role: str | None = None
    min_independent_sources: int | None = Field(default=None, ge=0)
    min_evidence_items: int | None = Field(default=None, ge=0)
    min_criteria_score: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_route: str | None = Field(default=None, max_length=32)
    v2_spec: dict[str, Any] | None = None
    notes: str | None = None


def _normalize_v2_upload_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten a v2-schema case dict (eval_case_schema.json case_template —
    nested routing/retrieval_requirements/synthesis_requirements/
    document_requirements/cost_latency_budget/adversarial_properties/
    harness_integrity_checks sections) into EvalCaseUploadItem's flat shape,
    packing the nested sections into v2_spec for scoring functions to read.

    Also accepts the existing flat shape (eval_cases_routing_seed.json) as-is —
    a dict with no v2 nested keys passes through with v2_spec=None.
    """
    v2_keys = (
        "routing", "retrieval_requirements", "synthesis_requirements",
        "document_requirements", "cost_latency_budget", "adversarial_properties",
        "harness_integrity_checks",
    )
    if not any(k in raw for k in v2_keys):
        return raw  # already flat (or has no v2 sections to extract)

    routing = raw.get("routing") or {}
    flat = {
        "title": raw.get("title"),
        "query": raw.get("query"),
        "category": raw.get("category"),
        "expected_criteria": raw.get("expected_criteria") or [],
        "expected_primary_role": raw.get("expected_primary_role"),
        "notes": raw.get("notes"),
        "expected_route": routing.get("expected_route"),
        "v2_spec": {
            **{k: raw[k] for k in v2_keys if raw.get(k) is not None},
            # prior_context is a top-level case field (not a v2_keys section) but
            # needs to be stored somewhere — no dedicated DB column, so it goes into
            # v2_spec so _run_one_eval_case can pass it to the orchestrator.
            **({} if raw.get("prior_context") is None else {"prior_context": raw["prior_context"]}),
        },
    }
    retrieval = raw.get("retrieval_requirements") or {}
    if retrieval.get("min_independent_sources") is not None:
        flat["min_independent_sources"] = retrieval["min_independent_sources"]
    if retrieval.get("min_evidence_items") is not None:
        flat["min_evidence_items"] = retrieval["min_evidence_items"]
    harness_checks = raw.get("harness_integrity_checks") or {}
    band = harness_checks.get("expected_judge_score_band")
    if band:
        flat["min_criteria_score"] = band[0]
    return flat


@router.post("/cases/upload", status_code=200)
def upload_eval_cases(
    body: Any = Body(...),
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """Bulk upsert eval cases from a JSON array (or {"cases": [...]} wrapper,
    matching eval_case_schema.json's top-level shape).

    Accepts both the original flat case shape and the v2 nested schema
    (routing/retrieval_requirements/etc. — see _normalize_v2_upload_item);
    each item is detected and normalized independently, so a single upload
    can mix both shapes.

    Matching is by title (case-insensitive). Existing active cases are updated;
    inactive (soft-deleted) cases are reactivated and updated; new titles are created.
    Returns counts: created / updated / reactivated.
    """
    from app.db.models import EvalCase, SessionLocal

    raw_items = body.get("cases", []) if isinstance(body, dict) else body
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=422, detail="Body must be a JSON array of cases, or {\"cases\": [...]}.")

    db = SessionLocal()
    created = updated = reactivated = 0
    errors: list[dict] = []
    now = datetime.now(timezone.utc)

    try:
        # Build title → case map (all rows, including inactive, for upsert matching).
        existing: dict[str, EvalCase] = {
            c.title.lower(): c
            for c in db.query(EvalCase).all()
        }

        for raw_item in raw_items:
            try:
                item = EvalCaseUploadItem(**_normalize_v2_upload_item(raw_item))
                key = item.title.lower()
                case = existing.get(key)
                if case:
                    was_inactive = not case.is_active
                    case.title = item.title
                    case.query = item.query
                    case.category = item.category
                    case.expected_criteria_json = json.dumps(item.expected_criteria)
                    case.expected_primary_role = item.expected_primary_role
                    case.min_independent_sources = item.min_independent_sources
                    case.min_evidence_items = item.min_evidence_items
                    case.min_criteria_score = item.min_criteria_score
                    case.expected_route = item.expected_route
                    case.v2_spec_json = json.dumps(item.v2_spec) if item.v2_spec else None
                    case.notes = item.notes
                    case.is_active = True
                    case.updated_at = now
                    if was_inactive:
                        reactivated += 1
                    else:
                        updated += 1
                else:
                    new_case = EvalCase(
                        title=item.title,
                        query=item.query,
                        category=item.category,
                        expected_criteria_json=json.dumps(item.expected_criteria),
                        expected_primary_role=item.expected_primary_role,
                        min_independent_sources=item.min_independent_sources,
                        min_evidence_items=item.min_evidence_items,
                        min_criteria_score=item.min_criteria_score,
                        expected_route=item.expected_route,
                        v2_spec_json=json.dumps(item.v2_spec) if item.v2_spec else None,
                        notes=item.notes,
                        created_by=admin.user_id,
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(new_case)
                    created += 1
            except Exception as exc:
                errors.append({"title": raw_item.get("title", "?") if isinstance(raw_item, dict) else "?", "error": str(exc)})

        db.commit()
    finally:
        db.close()

    return {
        "created": created,
        "updated": updated,
        "reactivated": reactivated,
        "errors": errors,
    }


# ===========================================================================
# General eval runs (LangGraph + criteria scoring)
# ===========================================================================

# In-process registry for general eval runs.
_EVAL_RUNS: dict[str, dict[str, Any]] = {}
_EVAL_RUNS_LOCK = threading.Lock()


def _make_eval_run(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "running",    # running | complete | stopped | error
        "total": None,          # set once case list is loaded
        "completed": 0,         # incremented after each case finishes
        "progress": [],         # list of per-case result dicts (poll to watch growth)
        "log": [],              # human-readable lines for the UI log panel
        "results": None,        # final envelope (set on completion)
        "error": None,
        "stop_requested": False,  # set by POST /runs/{id}/stop
        "started_at": time.time(),
        "completed_at": None,
    }


def _get_eval_run(run_id: str) -> dict[str, Any]:
    with _EVAL_RUNS_LOCK:
        run = _EVAL_RUNS.get(run_id)
    if run is None:
        # Fall through to DB for historical runs
        raise HTTPException(status_code=404, detail=f"Eval run {run_id!r} not found.")
    return run


def _score_criteria(query: str, response_text: str, criteria: list[str]) -> dict[str, Any]:
    """Use a fast LLM call to score a response against expected criteria.

    Returns: {score: float 0-1, passed: list[str], failed: list[str], explanation: str}
    """
    if not criteria or not response_text:
        return {"score": None, "passed": [], "failed": [], "explanation": "No criteria defined."}

    criteria_block = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
    prompt = (
        f"Query: {query}\n\n"
        f"Response to evaluate:\n{response_text[:3000]}\n\n"
        f"Criteria:\n{criteria_block}\n\n"
        "For each criterion, output PASS or FAIL followed by a brief reason. "
        'Then output a JSON block: {"score": <0.0-1.0>, "passed": [...], "failed": [...], "explanation": "<one sentence>"}'
    )
    try:
        from app.services.agent import model_client
        result = model_client.simple_completion(
            system=(
                "You are an evaluation judge. Score a research response against specific criteria. "
                "Be strict but fair. Output structured JSON as instructed."
            ),
            user=prompt,
            max_tokens=600,
            role="direct_answer",
        )
        text = result.text
        # Extract the JSON block
        import re
        match = re.search(r'\{[^{}]*"score"[^{}]*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "score": float(data.get("score", 0.0)),
                "passed": data.get("passed", []),
                "failed": data.get("failed", []),
                "explanation": data.get("explanation", ""),
            }
    except Exception as exc:
        logger.warning("Criteria scoring failed: %s", exc)
    return {"score": None, "passed": [], "failed": [], "explanation": "Scoring failed."}


def _format_prior_context(prior_context: list[dict] | None) -> str:
    """Format a v2_spec prior_context list into the conversation_context string
    TurnRequest expects. The orchestrator reads conversation_context both in the
    LLM-based decide() prompt and in _referent_resolves_from_context() for the
    heuristic fallback — without this, multi-turn eval cases (e.g. #38, #58, #59)
    were always routed as if they had no history, causing false clarify responses."""
    if not prior_context:
        return ""
    lines = []
    for turn in prior_context:
        role = (turn.get("role") or "").capitalize()
        content = (turn.get("content") or "").strip()
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _decide_eval_route(query: str, prior_context: list[dict] | None = None):
    """Get the real orchestrator decision for an eval case query — the same
    routing call a real user turn makes. No force_route: the orchestrator
    picks direct/clarify/research/document/research_document on its own,
    exactly like Runtime.run_stream() does. This is what lets the harness
    grade routing correctness (expected_route) in addition to research-
    pipeline quality — previously every case was force-routed to "research"
    regardless of what the query actually called for.

    prior_context (from the v2 case spec) is formatted into conversation_context
    so the orchestrator sees the same conversational history it would in production.
    """
    from app.services.agent.models import TurnRequest
    from app.services.agent.orchestrator import decide

    draft = TurnRequest(
        message=query,
        research_level="auto",
        quality_mode="standard",
        output_format="chat",
        conversation_context=_format_prior_context(prior_context),
    )
    return decide(draft)


def _build_eval_request(query: str, decision, *, confirm_deep_research: bool, prior_context: list[dict] | None = None):
    """Build the TurnRequest to actually dispatch, given an already-computed
    `decision` (from _decide_eval_route) — never re-decides, so a case's
    routing grade and its dispatched request always agree with each other.

    research_level is resolved by choose_research_level() (via decide()'s
    normal path) whenever the decision lands on research/research_document —
    same dimension-richness classifier production uses, not hardcoded.

    confirm_deep_research is the caller's choice, not hardcoded: pass False
    to test whether the deep-research confirmation gate (runtime.py:341,
    the "Continue with deep research?" prompt a real user sees) actually
    fires — see _check_deep_research_gate. Pass True to bypass it and grade
    the research that would run once a user approves, the same way clicking
    "Start research" does on a real second turn.
    """
    from app.services.agent.models import TurnRequest

    research_level = decision.research_level if decision.route in ("research", "research_document") else "auto"
    return TurnRequest(
        message=query,
        research_level=research_level,
        quality_mode="standard",
        output_format="chat",
        confirm_deep_research=confirm_deep_research,
        conversation_context=_format_prior_context(prior_context),
    )


def _check_deep_research_gate(unconfirmed_request, decision, tools) -> dict[str, Any]:
    """Verify the deep-research confirmation gate actually fires for a
    deep-tier-shaped query, by calling the SAME Runtime._run_deep_research_confirmation
    handler run_stream() uses (runtime.py:1157), with confirm_deep_research=False
    (no pre-approval) — i.e. exactly what a real user's FIRST turn produces
    before they've clicked anything.

    A correctly-firing gate returns route="clarify" with a non-empty
    research_plan_preview and a "Start research" follow-up option that would
    resume research with confirm_deep_research=True if clicked (or, with a
    timed gate, auto-fires after the countdown — same payload either way).
    """
    from app.services.agent.models import Goal, ProgressEvent, new_id
    from app.services.agent.runtime import Runtime

    turn_id = new_id("turn")
    events: list = []

    def progress(stage: str, message: str, **data) -> ProgressEvent:
        event = ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)
        events.append(event)
        return event

    runtime = Runtime(tools)
    goal = Goal(
        user_id="eval", conversation_id=unconfirmed_request.conversation_id,
        objective=unconfirmed_request.message, route=decision.route, quality_mode=unconfirmed_request.quality_mode,
    )
    result, _ = runtime._run_deep_research_confirmation(unconfirmed_request, goal, turn_id, events, decision, progress)

    preview = result.research_plan_preview or {}
    has_preview = bool(preview.get("workflow") or preview.get("investigate"))
    resumes_research = any(
        opt.get("confirm_deep_research") is True and opt.get("research_level") == "deep"
        for opt in (result.follow_up_options or [])
    )
    return {
        "route": result.route,
        "has_preview": has_preview,
        "resumes_research": resumes_research,
        "pass": result.route == "clarify" and has_preview and resumes_research,
    }


def _drain_generator(gen) -> Any:
    """Drive a generator-based Runtime route handler to completion and
    return its StopIteration.value (the handler's actual return value)."""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _run_research_subtree_blocking(request, tools) -> dict[str, Any]:
    """Drive Runtime._run_research_subtree() to completion and return its
    result dict — the same LangGraph research dispatch a real research-routed
    user turn goes through, without the outer run_stream() SSE/TurnResult
    wrapping (fast-path pre-routing and the deep-research confirmation gate
    don't apply here: the eval already pre-approves confirm_deep_research,
    same as _build_eval_request documents)."""
    from app.services.agent.models import ProgressEvent, new_id
    from app.services.agent.runtime import Runtime

    turn_id = new_id("turn")

    def progress(stage: str, message: str, **data) -> ProgressEvent:
        return ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)

    runtime = Runtime(tools)
    return _drain_generator(runtime._run_research_subtree(request, progress))


def _run_research_document_blocking(request, tools) -> dict[str, Any]:
    """research_document is a TWO-STAGE route in production (runtime.py:378-392):
    research first, then _run_document writes a downloadable artifact FROM
    that research's answer + evidence (research_answer=research["response"].text,
    evidence=research["evidence"]). The eval harness previously dispatched
    research_document through _run_research_subtree_blocking alone (same as
    plain "research"), which never called _run_document at all — silently
    testing only the research half of this route and never actually
    producing a document artifact to grade. Fixed here to match production's
    exact two-stage flow.
    """
    import types

    from app.services.agent.models import Goal, ProgressEvent, new_id
    from app.services.agent.runtime import Runtime

    research = _run_research_subtree_blocking(request, tools)

    turn_id = new_id("turn")
    events: list = []

    def progress(stage: str, message: str, **data) -> ProgressEvent:
        event = ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)
        events.append(event)
        return event

    runtime = Runtime(tools)
    goal = Goal(
        user_id="eval", conversation_id=request.conversation_id,
        objective=request.message, route="research_document", quality_mode=request.quality_mode,
    )
    research_response = research.get("response")
    research_answer_text = research_response.text if hasattr(research_response, "text") else ""
    doc_result = _drain_generator(runtime._run_document(
        request, goal, turn_id, events, progress,
        sources=research.get("sources") or [],
        research_answer=research_answer_text,
        evidence=research.get("evidence"),
    ))

    response_shim = types.SimpleNamespace(
        text=doc_result.answer,
        model_used=doc_result.model_used,
        latency_ms=(getattr(research_response, "latency_ms", 0) or 0) + (doc_result.latency_ms or 0),
        cost_usd=(getattr(research_response, "cost_usd", 0.0) or 0.0) + (doc_result.cost_usd or 0.0),
    )
    return {
        "response": response_shim,
        "sources": doc_result.sources,
        "tool_calls": [*(research.get("tool_calls") or []), *doc_result.tool_calls],
        "artifacts": doc_result.artifacts,
        # Preserve the rich research evidence/judge feedback for grounding,
        # retrieval-completeness, and gap-honesty checks — the document
        # stage itself doesn't bind new evidence, it writes FROM this.
        "evidence": research.get("evidence"),
        "feedback": research.get("feedback"),
    }


def _run_non_research_route_blocking(request, decision, route: str, tools) -> dict[str, Any]:
    """Dispatch a direct/clarify/document-routed eval case through the same
    Runtime handler methods run_stream() uses for that route — without
    re-deciding the route (that would risk a second, possibly different,
    LLM routing call disagreeing with the `decision` already used for
    expected_route grading) and without run_stream()'s fast-path pre-check
    (which doesn't apply: the route is already decided).

    Returns a dict shaped like _run_research_subtree_blocking's — a
    {"response": <has .text/.model_used/.latency_ms/.cost_usd>, "sources": [...],
    "tool_calls": [...]} — so _run_one_eval_case's extraction code (written
    for the research path's response shape) works unchanged for every route.
    research_document still goes through the research path entirely; this
    only handles direct/clarify/document.
    """
    import types

    from app.services.agent.models import Goal, ProgressEvent, new_id
    from app.services.agent.runtime import Runtime

    turn_id = new_id("turn")
    events: list = []

    def progress(stage: str, message: str, **data) -> ProgressEvent:
        event = ProgressEvent(turn_id=turn_id, stage=stage, message=message, data=data)
        events.append(event)
        return event

    runtime = Runtime(tools)
    goal = Goal(user_id="eval", conversation_id=request.conversation_id, objective=request.message, route=route, quality_mode=request.quality_mode)

    if route == "clarify":
        result = runtime._run_clarify(request, goal, turn_id, events, decision)
    elif route == "direct":
        result = _drain_generator(runtime._run_direct(request, goal, turn_id, events, progress))
    elif route == "document":
        result = _drain_generator(runtime._run_document(request, goal, turn_id, events, progress, sources=[]))
    else:
        raise ValueError(f"_run_non_research_route_blocking does not handle route={route!r}")

    response_shim = types.SimpleNamespace(
        text=result.answer, model_used=result.model_used, latency_ms=result.latency_ms, cost_usd=result.cost_usd,
    )
    return {
        "response": response_shim, "sources": result.sources, "tool_calls": result.tool_calls,
        "artifacts": result.artifacts,
    }


def check_judge_structural_agreement(judge_score: float | None, answer_length: int) -> bool:
    """Harness integrity gate (scoring_spec.md §1.9).

    The pipeline's internal judge_score must not be confidently high while the
    answer is empty — that combination means the judge scored something other
    than the actual answer, or never ran at all and a fallback masqueraded as
    a real score (see PR #31's root cause: budget_gate_pre_synthesis routing
    to END before synthesize/judge ever ran, with the old fallback defaulting
    to score=1.0). Checked independently of trusting that upstream fix holds
    forever — this is a defense-in-depth structural check, not a product-
    quality judgment. Returns False (disagreement detected) when judge_score
    > 0.5 and the answer is empty; True otherwise (including judge_score is
    None, e.g. direct/clarify/document routes that never run a research judge).
    """
    non_empty_answer = answer_length > 0
    return not (judge_score is not None and judge_score > 0.5 and not non_empty_answer)


_RETRIEVAL_COMPLETENESS_PASS_THRESHOLD = 0.8  # scoring_spec.md §1.2 default


def compute_overall_status(
    *,
    judge_structural_agreement: bool,
    overall_structural_pass: bool,
    overall_benchmark_pass: bool | None,
    route_correct: bool | None,
    deep_research_gate: dict[str, Any] | None,
    scores: dict[str, Any] | None = None,
) -> str:
    """Roll up a case's independent axis results into one of
    pass | fail | partial | harness_error (eval_case_schema.json's
    result_record_template.overall_status).

    harness_error takes priority over everything else: a judge_structural_agreement
    failure means the result data itself is untrustworthy, so the case can't be
    meaningfully scored pass/fail/partial at all — it must never be averaged into
    product-quality dashboards alongside genuine pass/fail/partial results.

    v2 axes (retrieval_completeness, format_correct, must_not_recommend_ok) now
    contribute to the rollup — previously they were computed and surfaced in
    `scores` but never fed into overall_status, which let cases with
    retrieval_completeness=0.0 on a 36-item research result resolve to "pass".
    """
    if not judge_structural_agreement:
        return "harness_error"
    if not overall_structural_pass:
        return "fail"
    sc = scores or {}
    v2_partial = (
        # retrieval_completeness below threshold is a genuine coverage failure
        (sc.get("retrieval_completeness") is not None
         and sc["retrieval_completeness"] < _RETRIEVAL_COMPLETENESS_PASS_THRESHOLD)
        # format_correct=False means the document artifact doesn't meet spec
        or sc.get("format_correct") is False
        # must_not_recommend_ok=False is a hard quality rule violation
        or sc.get("must_not_recommend_ok") is False
        # gap_honesty and conflict_handling failures are quality findings
        or sc.get("gap_honesty") is False
        or sc.get("conflict_handling") is False
    )
    if (
        overall_benchmark_pass is False
        or route_correct is False
        or (deep_research_gate is not None and not deep_research_gate.get("pass"))
        or v2_partial
    ):
        return "partial"
    return "pass"


def _run_one_eval_case(case_dict: dict, tools, pipeline: str = "langgraph") -> dict[str, Any]:
    """Run a single eval case through ONE pipeline and grade it against the
    case's pre-determined expected_criteria (ground truth). The production
    research runtime is LangGraph-only; `pipeline` is retained only as an
    output label for persisted historical shape compatibility.
    """
    import traceback as tb

    query = case_dict["query"]
    criteria = case_dict.get("expected_criteria") or []
    # Extract prior_context from v2_spec for multi-turn cases — formatted into
    # conversation_context when building TurnRequests so both the LLM-based
    # orchestrator path and the heuristic fallback see the same history a real
    # user's session would provide. Without this, referential queries like
    # "The Salesforce one" always arrived at the orchestrator as if context-free,
    # producing false clarify responses regardless of what the case specified.
    v2_prior = (case_dict.get("v2_spec") or {}).get("prior_context")
    prior_context_turns: list[dict] | None = v2_prior if isinstance(v2_prior, list) else None

    # Fixture injection — harness integrity probe cases (eval_case_schema.json
    # case_id 120). The query starts with "[HARNESS-ONLY]" as a marker that the
    # case must NOT touch the model. Instead, inject answer="" and judge_score=1.0
    # directly into a canned result record so the judge_structural_agreement gate
    # (§1.9) is exercised against known-synthetic data. If this case ever reaches
    # the model, the probe is broken — as confirmed in evalrun_34691bb17fdf.json
    # where case #47 returned a live clarify response instead of harness_error.
    if query.strip().startswith("[HARNESS-ONLY]"):
        v2_fi = case_dict.get("v2_spec") or {}
        expected_route_fi = (v2_fi.get("routing") or {}).get("expected_route") or case_dict.get("expected_route")
        injected_run = {
            "ok": True, "error": None, "answer": "", "answer_length": 0,
            "evidence_count": 0, "claim_count": 0, "independent_source_count": 0,
            "judge_score": 1.0, "latency_ms": 0, "pipeline": pipeline,
            "criteria": {"score": 0.0, "feedback": "fixture injection — model not called"},
        }
        injected_structural = {"non_empty_answer": False}
        judge_structural_agreement = check_judge_structural_agreement(1.0, 0)
        overall_status = compute_overall_status(
            judge_structural_agreement=judge_structural_agreement,
            overall_structural_pass=False,
            overall_benchmark_pass=None,
            route_correct=None,
            deep_research_gate=None,
        )
        return {
            "case_id": case_dict["id"],
            "title": case_dict["title"],
            "query": query,
            "pipeline": pipeline,
            "route": "fixture",
            "research_level": None,
            "expected_route": expected_route_fi,
            "route_correct": None,
            "deep_research_gate": None,
            "is_canary": bool((v2_fi.get("harness_integrity_checks") or {}).get("is_canary")),
            "canary_drift": None,
            "run": injected_run,
            "structural": injected_structural,
            "benchmarks": {},
            "scores": {k: None for k in (
                "route_correct", "gate_correct", "retrieval_completeness",
                "retrieval_independence", "latency_pass", "synthesis_grounding",
                "gap_honesty", "conflict_handling", "must_not_recommend_ok",
                "answer_length_ok", "format_correct",
            )},
            "overall_structural_pass": False,
            "overall_benchmark_pass": None,
            "judge_structural_agreement": judge_structural_agreement,
            "overall_status": overall_status,
        }

    decision = _decide_eval_route(query, prior_context=prior_context_turns)
    route = decision.route

    # Two-pass deep-research gate test: first prove the gate actually fires
    # for a deep-tier-shaped query exactly as a real user's first turn would
    # see it (confirm_deep_research=False, no pre-approval), then dispatch
    # the real (approved) request below to grade the research itself. Both
    # passes use the SAME decision, so they can't disagree with each other.
    deep_research_gate = None
    if route in ("research", "research_document") and decision.requires_confirmation:
        try:
            unconfirmed_request = _build_eval_request(query, decision, confirm_deep_research=False, prior_context=prior_context_turns)
            deep_research_gate = _check_deep_research_gate(unconfirmed_request, decision, tools)
        except Exception:
            deep_research_gate = {"route": None, "has_preview": False, "resumes_research": False,
                                   "pass": False, "error": tb.format_exc()[:500]}

    request = _build_eval_request(query, decision, confirm_deep_research=True, prior_context=prior_context_turns)

    t0 = time.perf_counter()
    err = None
    result = None
    try:
        if route == "research":
            result = _run_research_subtree_blocking(request, tools)
        elif route == "research_document":
            result = _run_research_document_blocking(request, tools)
        else:
            result = _run_non_research_route_blocking(request, decision, route, tools)
    except Exception:
        err = tb.format_exc()
        logger.error(
            "eval case %s pipeline crash (route=%s): %s",
            case_dict.get("id"), route, err,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    evidence_items: list = []
    artifacts: list = []
    full_answer = ""  # untruncated — run["answer"] below is a display-length preview only
    if err or result is None:
        # Store the tail of the traceback, not the head — the exception type
        # and message are always at the end. [:500] was cutting them off,
        # leaving only module import frames and making every crash indistinguishable.
        run = {"ok": False, "error": (err or "")[-2000:], "answer": "", "answer_length": 0,
               "evidence_count": 0, "claim_count": 0, "judge_score": None}
    else:
        response = result.get("response")
        full_answer = response.text if hasattr(response, "text") else str(response or "")
        evidence = result.get("evidence")
        feedback = result.get("feedback")
        evidence_items = list(evidence.items) if evidence and hasattr(evidence, "items") else []
        artifacts = list(result.get("artifacts") or [])
        run = {
            "ok": True,
            "error": None,
            "answer": full_answer[:2000],
            "answer_length": len(full_answer),
            "evidence_count": len(evidence_items),
            "claim_count": len(evidence.claims) if evidence and hasattr(evidence, "claims") else 0,
            "independent_source_count": getattr(evidence, "independent_source_count", None) if evidence else None,
            "judge_score": getattr(feedback, "final_score", None) if feedback else None,
        }
    run["latency_ms"] = latency_ms
    run["pipeline"] = pipeline

    structural = {"ok": run["ok"], "non_empty_answer": run["answer_length"] > 0}
    if route in ("research", "research_document"):
        # has_evidence only makes sense (and is only required) for routes
        # that actually do research — a direct/clarify/document answer
        # correctly has zero evidence items, that's not a structural failure.
        structural["has_evidence"] = run["evidence_count"] > 0
    run["criteria"] = _score_criteria(query, run["answer"], criteria) if criteria and run["ok"] else None

    benchmarks = _score_benchmarks(case_dict, run)

    # scoring_spec.md §1.7 — for document-route cases with
    # extract_and_grade_content=true, grade the document's ACTUAL extracted
    # text, not the chat confirmation message ("Done. I created X.docx") —
    # the v1 harness graded that confirmation stub and got meaningless
    # 0.3-0.5 scores reflecting an inability to verify, not real document
    # quality. document_text is None (not "") when extraction wasn't
    # attempted/failed, so graded_text correctly falls back to full_answer
    # rather than grading an artificially empty string.
    v2_spec = case_dict.get("v2_spec") or {}
    document_text = None
    if route in ("document", "research_document") and (v2_spec.get("document_requirements") or {}).get("extract_and_grade_content") and artifacts:
        document_text = _extract_artifact_text(artifacts[0])
    graded_text = document_text if document_text is not None else full_answer

    expected_route = case_dict.get("expected_route")
    route_correct = (route == expected_route) if expected_route else None
    gate_correct = score_gate_correct(case_dict, deep_research_gate)
    # scoring_spec.md §3 n/a semantics: retrieval and synthesis axes don't apply
    # to non-retrieval routes — direct/clarify/fixture never fetch evidence, so
    # retrieval_completeness=0.0 would be a false failure, not a real coverage gap.
    # These axes return None (n/a) for these routes, and the dashboard correctly
    # shows "n/a" in the Direct/Clarify columns rather than "0%".
    _is_retrieval_route = route in ("research", "research_document", "document")
    retrieval_completeness = score_retrieval_completeness(case_dict, evidence_items) if _is_retrieval_route else None
    retrieval_independence = score_retrieval_independence(case_dict, run, evidence_items) if _is_retrieval_route else None
    latency_pass = score_latency_pass(case_dict, route, decision.research_level, latency_ms)
    synthesis_grounding = score_synthesis_grounding(graded_text, evidence_items) if _is_retrieval_route else None
    gap_honesty = score_gap_honesty(case_dict, graded_text, evidence_items) if _is_retrieval_route else None
    conflict_handling = score_conflict_handling(case_dict, graded_text, evidence_items) if _is_retrieval_route else None
    must_not_recommend_ok = score_must_not_recommend(case_dict, graded_text)
    answer_length_ok = score_answer_length_bounds(case_dict, len(graded_text) if document_text is not None else run["answer_length"])
    format_correct = score_format_correct(case_dict, artifacts)

    overall_structural_pass = all(structural.values())
    overall_benchmark_pass = all(b["pass"] for b in benchmarks.values()) if benchmarks else None
    judge_structural_agreement = check_judge_structural_agreement(run.get("judge_score"), run["answer_length"])
    scores_for_rollup = {
        "retrieval_completeness": retrieval_completeness,
        "format_correct": format_correct,
        "must_not_recommend_ok": must_not_recommend_ok,
        "gap_honesty": gap_honesty,
        "conflict_handling": conflict_handling,
    }
    # Pipeline crash: run.ok=False + empty answer is always harness_error regardless
    # of judge_score (which is None for direct/clarify routes and would bypass the
    # existing judge_structural_agreement gate). The existing gate only catches
    # judge_score>0.5 + empty_answer; it cannot catch ok=False + null judge_score.
    if not run["ok"] and run["answer_length"] == 0:
        overall_status = "harness_error"
    else:
        overall_status = compute_overall_status(
            judge_structural_agreement=judge_structural_agreement,
            overall_structural_pass=overall_structural_pass,
            overall_benchmark_pass=overall_benchmark_pass,
            route_correct=route_correct,
            deep_research_gate=deep_research_gate,
            scores=scores_for_rollup,
        )
    retrieval_completeness_pass = (
        retrieval_completeness >= _RETRIEVAL_COMPLETENESS_PASS_THRESHOLD
        if retrieval_completeness is not None else None
    )
    all_scores = {
        "route_correct": route_correct,
        "gate_correct": gate_correct,
        "retrieval_completeness": retrieval_completeness,
        "retrieval_completeness_pass": retrieval_completeness_pass,
        "retrieval_independence": retrieval_independence,
        "latency_pass": latency_pass,
        "synthesis_grounding": synthesis_grounding,
        "gap_honesty": gap_honesty,
        "conflict_handling": conflict_handling,
        "must_not_recommend_ok": must_not_recommend_ok,
        "answer_length_ok": answer_length_ok,
        "format_correct": format_correct,
    }
    canary_drift = score_canary_drift(case_dict, run.get("judge_score"), scores=all_scores)
    is_canary = bool(((v2_spec.get("harness_integrity_checks") or {}).get("is_canary")))

    return {
        "case_id": case_dict["id"],
        "title": case_dict["title"],
        "query": query,
        "pipeline": pipeline,
        "route": route,
        "research_level": decision.research_level,
        "expected_route": expected_route,
        "route_correct": route_correct,
        "deep_research_gate": deep_research_gate,
        "is_canary": is_canary,
        "canary_drift": canary_drift,
        "run": run,
        "structural": structural,
        "benchmarks": benchmarks,
        # scoring_spec.md §3 axis names — independent of overall_status,
        # never collapsed into one number (see §0 on why v1's blended
        # criteria.score hid the judge-decoupling and retrieval defects).
        "scores": all_scores,
        "overall_structural_pass": overall_structural_pass,
        "overall_benchmark_pass": overall_benchmark_pass,
        "judge_structural_agreement": judge_structural_agreement,
        "overall_status": overall_status,
    }


# scoring_spec.md §1.8 — default latency ceilings by route/tier. A case's own
# v2_spec.cost_latency_budget.latency_ms_ceiling overrides this when set.
_TIER_CEILING_MS = {
    "direct": 2000, "clarify": 2000,
    "research_easy": 20000, "research_regular": 60000, "research_deep": 300000,
    "document": 30000, "research_document": 120000,
}


def score_gate_correct(case_dict: dict, deep_research_gate: dict[str, Any] | None) -> bool | None:
    """scoring_spec.md §1.1 — compares deep_research_gate's actual firing
    against the case's routing.expected_gate_fires / expected_gate_silent.
    None if the case asserts nothing about gate behavior."""
    v2 = case_dict.get("v2_spec") or {}
    routing = v2.get("routing") or {}
    expected_fires = routing.get("expected_gate_fires")
    expected_silent = routing.get("expected_gate_silent")
    if expected_fires is None and expected_silent is None:
        return None
    fired = deep_research_gate is not None
    if expected_fires:
        return fired and bool(deep_research_gate.get("pass"))
    if expected_silent or expected_fires is False:
        return not fired
    return None


def _coverage_by_subject(case_dict: dict, evidence_items: list) -> dict[str, float] | None:
    """Per-subject fill ratio across required_dimensions — shared by
    score_retrieval_completeness (the aggregate) and score_gap_honesty
    (which needs to know WHICH subjects are incomplete, not just the
    overall ratio, to know which subjects' gap-disclosure to check)."""
    v2 = case_dict.get("v2_spec") or {}
    req = v2.get("retrieval_requirements") or {}
    subjects = req.get("required_subjects")
    dimensions = req.get("required_dimensions")
    if not subjects or not dimensions:
        return None
    # Build one searchable string per evidence item. Include item.question
    # (the targeted research question for this item) because it's specifically
    # generated against named subjects — "What is the data durability of AWS S3?"
    # — and is more reliable than hoping the product name appears verbatim in
    # scraped URL/title/body text (which often says "Amazon S3" not "AWS S3",
    # or abbreviates product names). item.query is the raw search-engine query
    # and also subject-targeted; both are included for coverage.
    haystacks = [
        f"{getattr(item, 'url', '')} {getattr(item, 'title', '')} "
        f"{getattr(item, 'evidence', '')} {getattr(item, 'query', '')} "
        f"{getattr(item, 'question', '')}".lower()
        for item in evidence_items
    ]
    result: dict[str, float] = {}
    for subject in subjects:
        subject_l = subject.lower()
        filled = sum(
            1 for dimension in dimensions
            if any(subject_l in h and dimension.lower() in h for h in haystacks)
        )
        result[subject] = filled / len(dimensions) if dimensions else 0.0
    return result


_PLACEHOLDER_SUBJECT_RE = re.compile(
    r"^(crm|ehr|reit|fund|platform|vendor|tool|framework|provider|service|system)\s+(platform|fund|vendor|tool|framework|provider|service|system)?\s*\d+$",
    re.IGNORECASE,
)


def _has_placeholder_subjects(subjects: list[str]) -> bool:
    """Return True if required_subjects look like runtime placeholders
    ('CRM platform 1', 'REIT fund 1', etc.) that can never appear verbatim
    in evidence text and would always produce retrieval_completeness=0.0
    for any real run, making the score meaningless rather than informative."""
    return any(_PLACEHOLDER_SUBJECT_RE.match(s.strip()) for s in subjects)


def score_retrieval_completeness(case_dict: dict, evidence_items: list) -> float | None:
    """scoring_spec.md §1.2 — coverage_cells_filled / coverage_cells_required.

    Matches each evidence item's url/title/content/originating-query against
    each required (subject, dimension) pair — not the rendered answer's prose,
    which can claim coverage it doesn't have. None if the case doesn't set
    required_subjects/required_dimensions.

    Also returns None (not 0.0) when required_subjects contains placeholders
    like 'CRM platform 1' / 'REIT fund 1' — those can never appear verbatim in
    evidence text, so 0.0 would be a misleading computation artifact, not a real
    coverage signal. The v2 starter set uses placeholders for cases where the
    researched subjects are determined at runtime (e.g. "compare any three CRM
    platforms") rather than pre-specified. Those cases need a different coverage
    strategy (entity-extraction after the run) that's not yet implemented.
    """
    by_subject = _coverage_by_subject(case_dict, evidence_items)
    if by_subject is None:
        return None
    subjects = list(by_subject.keys())
    if _has_placeholder_subjects(subjects):
        return None
    v2 = case_dict.get("v2_spec") or {}
    dimensions = (v2.get("retrieval_requirements") or {}).get("required_dimensions") or []
    if not by_subject or not dimensions:
        return None
    # Each subject's ratio is filled_dims/len(dimensions); the aggregate is
    # total filled cells / total required cells, i.e. the mean of per-subject
    # ratios weighted equally since every subject has the same dimension count.
    return sum(by_subject.values()) / len(by_subject)


def score_retrieval_independence(case_dict: dict, run: dict, evidence_items: list) -> bool | None:
    """scoring_spec.md §1.3 — min_independent_sources AND max_single_domain_share
    must both hold (when set). max_single_domain_share formalizes why e.g. 6
    evidence items from one forum domain (independent_source_count=1) is a
    fail, not a borderline case: if any single domain accounts for more than
    max_single_domain_share of evidence_count, fail regardless of the
    independent_source_count figure. None if the case sets neither threshold."""
    v2 = case_dict.get("v2_spec") or {}
    req = v2.get("retrieval_requirements") or {}
    min_sources = req.get("min_independent_sources") or case_dict.get("min_independent_sources")
    max_share = req.get("max_single_domain_share")
    if min_sources is None and max_share is None:
        return None

    ok = True
    if min_sources is not None:
        independent_count = run.get("independent_source_count")
        ok = ok and independent_count is not None and independent_count >= min_sources

    if max_share is not None and evidence_items:
        from urllib.parse import urlparse
        domain_counts: dict[str, int] = {}
        for item in evidence_items:
            domain = getattr(item, "source_family", "") or urlparse(getattr(item, "url", "")).netloc or "unknown"
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if domain_counts:
            share = max(domain_counts.values()) / len(evidence_items)
            if share > max_share:
                ok = False

    return ok


def score_latency_pass(case_dict: dict, route: str, research_level: str | None, latency_ms: int) -> bool:
    """scoring_spec.md §1.8 — latency against a tiered SLA ceiling. Always
    computed (no opt-out): every case has a route and therefore a ceiling,
    so this is never None — v1 had latencies from 595ms to 309s with zero
    ceiling attached to any of it."""
    v2 = case_dict.get("v2_spec") or {}
    budget = v2.get("cost_latency_budget") or {}
    ceiling = budget.get("latency_ms_ceiling")
    if ceiling is None:
        key = f"research_{research_level}" if route in ("research",) and research_level else route
        ceiling = _TIER_CEILING_MS.get(key, _TIER_CEILING_MS.get(route, 60000))
    return latency_ms <= ceiling


def score_synthesis_grounding(answer: str, evidence_items: list) -> float | None:
    """scoring_spec.md §1.5 — citation marker [S#] cross-check against the
    actual evidence manifest for this run, not an LLM judge reading prose in
    isolation (which can't reliably catch fabricated-but-plausible citations
    since it has no evidence pack to cross-check against).

    EvidenceItem.source_id is assigned sequentially as "S1", "S2", ... during
    bind_evidence (research_evidence.py:406) — the [S#] bracket marker IS the
    source_id, so this is an exact membership check, not fuzzy matching.

    "claims" here means distinct citation markers used in the answer, not
    sentence-level claim segmentation (open question — true claim→citation
    mapping would need parsing each sentence's citations individually; this
    is the citation-index-validity subset of §1.5, which is what catches the
    fabricated-citation failure mode the section is primarily about).

    Returns None if the answer has no citation markers at all — distinct
    from 0.0, which means citations exist but are ALL invalid (fabricated).
    """
    cited_indices = set(re.findall(r"\[S(\d+)\]", answer or ""))
    if not cited_indices:
        return None
    valid_source_ids = {getattr(item, "source_id", "") for item in evidence_items}
    valid = sum(1 for idx in cited_indices if f"S{idx}" in valid_source_ids)
    return valid / len(cited_indices)


def _render_evidence_manifest(evidence_items: list, max_items: int = 25) -> str:
    """Compact text rendering of the evidence pack for judge prompts —
    scoring_spec.md §1.6's "feed the judge the evidence pack alongside the
    answer, not just the answer text in isolation." Without this, a gap-
    honesty or conflict-handling judgment is just trusting the answer's own
    self-report; with it, the judge can verify a claimed gap is real (or a
    claimed conflict actually exists in the sources) rather than taking the
    answer's word for it."""
    if not evidence_items:
        return "(no evidence items in this run)"
    lines = []
    for item in evidence_items[:max_items]:
        source_id = getattr(item, "source_id", "?")
        title = (getattr(item, "title", "") or "")[:80]
        url = getattr(item, "url", "")
        snippet = (getattr(item, "evidence", "") or "")[:200].replace("\n", " ")
        lines.append(f"[{source_id}] {title} ({url})\n  {snippet}")
    if len(evidence_items) > max_items:
        lines.append(f"... and {len(evidence_items) - max_items} more evidence item(s) not shown.")
    return "\n".join(lines)


def _binary_judge_call(question: str, answer: str, evidence_items: list) -> bool | None:
    """scoring_spec.md §1.6 — narrow, single-question yes/no judge call,
    evidence pack included. Binary-per-subject judge calls are more reliable
    than one holistic 0-1 score over a whole multi-thousand-word answer
    (the open-ended expected_criteria approach this is replacing for
    gap-honesty/conflict-handling specifically — other axes still use the
    broader criteria judge where a narrow yes/no doesn't fit).

    Returns None on a judge call failure (distinct from False — a failed
    judge call is a harness issue, not a "no" answer) so callers can avoid
    treating an infrastructure failure as a confident negative finding.
    """
    if not answer:
        return None
    prompt = (
        f"Evidence available to the research system for this answer:\n{_render_evidence_manifest(evidence_items)}\n\n"
        f"Answer to evaluate:\n{answer[:4000]}\n\n"
        f"Question: {question}\n\n"
        'Answer with exactly one word, "YES" or "NO", on its own — no explanation.'
    )
    try:
        from app.services.agent import model_client
        result = model_client.simple_completion(
            system=(
                "You are a narrow, binary evaluation judge. You will be given the evidence a research "
                "system had access to and the answer it produced, then asked one yes/no question. "
                "Cross-check the answer against the evidence — don't just trust the answer's own claims. "
                "Respond with exactly YES or NO."
            ),
            user=prompt,
            max_tokens=10,
            role="direct_answer",
        )
        text = (result.text or "").strip().upper()
        if "YES" in text:
            return True
        if "NO" in text:
            return False
        return None
    except Exception:
        return None


def score_gap_honesty(case_dict: dict, answer: str, evidence_items: list) -> bool | None:
    """scoring_spec.md §1.6 — for each required_subject whose
    retrieval_completeness is < 1.0, the answer must explicitly disclose
    that gap rather than omitting it silently. Returns True/False (all
    incomplete subjects' gaps disclosed, or not) — None if the case doesn't
    set synthesis_requirements.must_disclose_gaps, or if every subject's
    coverage is already complete (no gaps to disclose)."""
    v2 = case_dict.get("v2_spec") or {}
    if not (v2.get("synthesis_requirements") or {}).get("must_disclose_gaps"):
        return None
    by_subject = _coverage_by_subject(case_dict, evidence_items)
    if not by_subject:
        return None
    incomplete = [subject for subject, ratio in by_subject.items() if ratio < 1.0]
    if not incomplete:
        return None  # nothing incomplete, so nothing to check disclosure of
    results = [
        _binary_judge_call(
            f'Does the answer explicitly state that evidence/coverage for "{subject}" is incomplete, '
            f"missing, or thin — rather than silently omitting that gap?",
            answer, evidence_items,
        )
        for subject in incomplete
    ]
    if any(r is None for r in results):
        return None  # a judge call failed — don't report a confident result
    return all(results)


def score_conflict_handling(case_dict: dict, answer: str, evidence_items: list) -> bool | None:
    """scoring_spec.md §1.6 — if sources disagree, the answer must name the
    disagreement by source position (official vs. practitioner, vendor A vs.
    vendor B) rather than silently averaging or picking one. None if the
    case doesn't set synthesis_requirements.must_surface_conflicts."""
    v2 = case_dict.get("v2_spec") or {}
    if not (v2.get("synthesis_requirements") or {}).get("must_surface_conflicts"):
        return None
    return _binary_judge_call(
        "If the evidence contains disagreeing sources (e.g. official guidance vs. practitioner reports, "
        "or different vendors/sources giving different figures), does the answer name that disagreement "
        "explicitly by source position, rather than silently merging/averaging the figures or picking one "
        "without acknowledging the conflict? (If the evidence has no real disagreement, answer YES — there's "
        "nothing to suppress.)",
        answer, evidence_items,
    )


def score_must_not_recommend(case_dict: dict, answer: str) -> bool | None:
    """scoring_spec.md §1.6/eval_case_schema.json — formal version of the
    adversarial-phrasing gap: a query that doesn't itself ask for a
    recommendation shouldn't volunteer one unprompted. Programmatic keyword
    check, not LLM — same rationale as the citation-marker regex in
    score_synthesis_grounding: cheap, deterministic, no judge-prompt
    variance for something this mechanically checkable."""
    v2 = case_dict.get("v2_spec") or {}
    if not (v2.get("synthesis_requirements") or {}).get("must_not_recommend"):
        return None
    text = (answer or "").lower()
    recommend_terms = (
        "i recommend", "we recommend", "recommended choice", "recommended option",
        "best choice", "best option", "you should choose", "you should pick",
        "the best provider", "the best platform", "the best framework", "the best service",
    )
    return not any(term in text for term in recommend_terms)


def score_answer_length_bounds(case_dict: dict, answer_length: int) -> bool | None:
    """eval_case_schema.json synthesis_requirements.max_answer_length/
    min_answer_length — flags over-research/over-verbosity on simple
    lookups (max) or suspiciously thin answers (min). None if the case
    sets neither bound."""
    v2 = case_dict.get("v2_spec") or {}
    req = v2.get("synthesis_requirements") or {}
    max_len = req.get("max_answer_length")
    min_len = req.get("min_answer_length")
    if max_len is None and min_len is None:
        return None
    if max_len is not None and answer_length > max_len:
        return False
    if min_len is not None and answer_length < min_len:
        return False
    return True


def _extract_artifact_text(artifact: Any) -> str | None:
    """scoring_spec.md §1.7 — open the generated document artifact and
    extract its real text, via the same document_extractor.extract_text()
    used elsewhere in the app for uploaded files. Returns None if there's no
    artifact, no base64_data, or extraction fails — callers must treat None
    as "couldn't verify", not as empty/failing content."""
    import base64

    from app.services.document_extractor import ExtractionError, extract_text

    base64_data = getattr(artifact, "base64_data", "") or ""
    if not base64_data:
        return None
    try:
        content = base64.b64decode(base64_data)
        filename = getattr(artifact, "filename", "") or f"document.{getattr(artifact, 'kind', 'docx')}"
        text, _pages, _truncated, _method = extract_text(filename, content)
        return text
    except ExtractionError:
        return None
    except Exception:
        return None


def score_format_correct(case_dict: dict, artifacts: list) -> bool | None:
    """scoring_spec.md §1.7 — verifies the generated artifact matches
    expected_format, and (if expected_page_count is set) roughly matches
    the expected length via a word-count proxy (~500 words/page, ±1 page
    slack) — the spec's own suggested fallback. The docx/pptx text
    extractor (document_extractor.py) doesn't compute real page counts
    (would need actual rendering), so a true page-count check isn't
    available; word-count is the closest honest proxy rather than a fake
    precise number. None if the case doesn't set expected_format.
    """
    v2 = case_dict.get("v2_spec") or {}
    doc_req = v2.get("document_requirements") or {}
    expected_format = doc_req.get("expected_format")
    if not expected_format:
        return None
    if not artifacts:
        return False
    artifact = artifacts[0]
    if getattr(artifact, "kind", None) != expected_format:
        return False
    expected_pages = doc_req.get("expected_page_count")
    if expected_pages is not None:
        text = _extract_artifact_text(artifact)
        if text is None:
            return False
        estimated_pages = max(1, round(len(text.split()) / 500))
        if abs(estimated_pages - expected_pages) > 1:
            return False
    return True


def score_canary_drift(
    case_dict: dict,
    judge_score: float | None,
    scores: dict[str, Any] | None = None,
) -> bool | None:
    """scoring_spec.md §2 — canary calibration check.

    Supports two canary patterns:

    1. Band check (old pattern, most canaries): `expected_judge_score_band`
       set, judge_score outside the band → drift. Used for known-pass and
       known-fail canaries that are calibrated against expected judge output.
       Returns None when judge_score unavailable (non-research routes).

    2. Primary-signal check (new pattern, inverted canaries): `canary_primary_signal`
       names a key in `scores` (e.g. "answer_length_ok"), and
       `canary_expected_primary_signal_value` is the EXPECTED value for that
       signal — e.g. False means "this canary is EXPECTED to fail this check."
       Drift fires when the actual signal value DIFFERS from the expected value,
       i.e. when the case unexpectedly PASSES a check it should fail (the
       over-research canary) or unexpectedly FAILS one it should pass. This is
       the inverse of the band pattern: the canary exists to catch the pipeline
       STOPPING to over-research, not to catch it starting to over-research.

    Returns True (drifted — investigate), False (within spec), or None (this
    case isn't a canary, or the relevant signal isn't available for this run).
    """
    v2 = case_dict.get("v2_spec") or {}
    checks = v2.get("harness_integrity_checks") or {}
    if not checks.get("is_canary"):
        return None

    # Pattern 2: primary-signal canary (bidirectional, judge-independent)
    primary_signal = checks.get("canary_primary_signal")
    if primary_signal is not None:
        if scores is None:
            return None
        expected_value = checks.get("canary_expected_primary_signal_value")
        actual_value = scores.get(primary_signal)
        if actual_value is None:
            return None  # signal not computed this run (wrong route type etc.)
        return actual_value != expected_value

    # Pattern 1: judge score band check (original pattern)
    band = checks.get("expected_judge_score_band")
    if not band or judge_score is None:
        return None
    lo, hi = band[0], band[1]
    return not (lo <= judge_score <= hi)


def _score_benchmarks(case_dict: dict, run: dict) -> dict[str, dict[str, Any]]:
    """Deterministic pass/fail against a case's structured benchmark
    thresholds (min_evidence_items, min_independent_sources,
    min_criteria_score) — distinct from _score_criteria's LLM judgment.
    Only thresholds the case actually sets are scored; an empty dict means
    the case has no structured benchmarks defined."""
    benchmarks: dict[str, dict[str, Any]] = {}

    min_evidence = case_dict.get("min_evidence_items")
    if min_evidence is not None:
        actual = run.get("evidence_count") or 0
        benchmarks["min_evidence_items"] = {"target": min_evidence, "actual": actual, "pass": actual >= min_evidence}

    min_sources = case_dict.get("min_independent_sources")
    if min_sources is not None:
        actual = run.get("independent_source_count")
        benchmarks["min_independent_sources"] = {
            "target": min_sources,
            "actual": actual,
            "pass": actual is not None and actual >= min_sources,
        }

    min_score = case_dict.get("min_criteria_score")
    if min_score is not None:
        criteria = run.get("criteria") or {}
        actual = criteria.get("score")
        benchmarks["min_criteria_score"] = {
            "target": min_score,
            "actual": actual,
            "pass": actual is not None and actual >= min_score,
        }

    return benchmarks


# scoring_spec.md §3 — axis x tier dashboard columns. research_level splits
# "research" into three columns since tier (easy/regular/deep) materially
# changes what's expected (budget, depth, latency ceiling) — collapsing
# them would hide exactly the kind of tier-specific regression the v1
# harness couldn't see at all.
_DASHBOARD_TIERS = [
    "direct", "clarify", "research_easy", "research_regular", "research_deep",
    "document", "research_document",
]

# (axis_key in the per-case "scores" dict, display label, "bool" | "float")
_DASHBOARD_AXES = [
    ("route_correct", "Route accuracy", "bool"),
    ("gate_correct", "Gate accuracy", "bool"),
    ("retrieval_completeness", "Retrieval completeness", "float"),
    ("retrieval_independence", "Retrieval independence", "bool"),
    ("synthesis_grounding", "Synthesis grounding", "float"),
    ("gap_honesty", "Gap honesty", "bool"),
    ("conflict_handling", "Conflict handling", "bool"),
    ("format_correct", "Format correctness", "bool"),
    ("latency_pass", "Latency SLA pass rate", "bool"),
]


def _dashboard_tier_key(case: dict) -> str:
    route = case.get("route")
    if route == "research":
        return f"research_{case.get('research_level') or 'regular'}"
    return route or "unknown"


def compute_dashboard(cases: list[dict]) -> dict:
    """scoring_spec.md §3 — axis x tier rollup + Harness Integrity panel.

    Purely a presentation-layer aggregation over already-computed per-case
    "scores" dicts — no new scoring logic, no model calls. Sequencing
    matters per the spec ("if integrity panel is red, don't read the rest
    of the dashboard"): cases with overall_status=="harness_error" are
    EXCLUDED from the table's aggregates entirely, not silently averaged
    in — folding a structurally-disagreeing result into an aggregate would
    dilute a real defect into a misleadingly decent-looking number instead
    of surfacing it in the integrity panel where it belongs.
    """
    harness_error_cases = [c for c in cases if c.get("overall_status") == "harness_error"]
    canary_drift_cases = [c for c in cases if c.get("canary_drift")]
    trustworthy_cases = [c for c in cases if c.get("overall_status") != "harness_error"]

    table: dict[str, dict[str, Any]] = {}
    for axis_key, axis_label, value_type in _DASHBOARD_AXES:
        by_tier: dict[str, Any] = {}
        for tier in _DASHBOARD_TIERS:
            values = [
                c["scores"][axis_key]
                for c in trustworthy_cases
                if _dashboard_tier_key(c) == tier and (c.get("scores") or {}).get(axis_key) is not None
            ]
            if not values:
                by_tier[tier] = None
            elif value_type == "bool":
                by_tier[tier] = {"rate": sum(1 for v in values if v) / len(values), "n": len(values)}
            else:
                by_tier[tier] = {"mean": sum(values) / len(values), "n": len(values)}
        table[axis_key] = {"label": axis_label, "by_tier": by_tier}

    return {
        "integrity": {
            "ok": not harness_error_cases and not canary_drift_cases,
            "harness_error_count": len(harness_error_cases),
            "harness_error_case_ids": [c["case_id"] for c in harness_error_cases],
            "canary_drift_count": len(canary_drift_cases),
            "canary_drift_case_ids": [c["case_id"] for c in canary_drift_cases],
        },
        "total_cases": len(cases),
        "trustworthy_cases": len(trustworthy_cases),
        "tiers": _DASHBOARD_TIERS,
        "table": table,
        # Cost band distribution (spec §1.8/§3) intentionally omitted — open
        # question #4: per-call token/dollar capture isn't wired up at the
        # LangGraph node level yet. Flagged as a prerequisite, not stubbed
        # with fake data.
    }


def _make_result_envelope(
    mode: str, cases: list, langsmith_summary: dict | None, pipeline: str = "langgraph"
) -> dict:
    """Consistent result envelope stored in memory and DB regardless of eval mode.

    Shape:
      {
        "mode": "langsmith" | "in_process",
        "pipeline": "langgraph" | "legacy", # which single pipeline these cases ran against
        "cases": [EvalCaseRunResult, ...],  # empty for LangSmith runs (LangSmith is source of truth)
        "langsmith": { ... } | null,        # LangSmith experiment summary, null for in-process
        "harness_integrity_ok": bool,       # false if ANY case hit overall_status=="harness_error"
        "harness_error_case_ids": [...],    # which cases, for direct lookup
      }

    harness_integrity_ok must be checked BEFORE trusting any pass/fail rate in
    this envelope (scoring_spec.md §3) — a harness_error means the pipeline's
    judge_score disagreed with the actual answer for that case (see
    judge_structural_agreement), which is a scoring/pipeline integrity defect,
    not a product-quality signal, and would corrupt any aggregate computed
    from raw judge_score/criteria.score values.

    This guarantees /runs/{run_id}/result always returns the same contract; the
    caller need not branch on mode to read per-case rows vs. a summary dict.
    """
    harness_error_case_ids = [c["case_id"] for c in cases if c.get("overall_status") == "harness_error"]
    return {
        "mode": mode,
        "pipeline": pipeline,
        "cases": cases,
        "langsmith": langsmith_summary,
        "harness_integrity_ok": len(harness_error_case_ids) == 0,
        "harness_error_case_ids": harness_error_case_ids,
    }


def _drain_ls_events_to_run(events_q: queue.Queue, run: dict) -> None:
    """Drain LangSmith event queue into run['log'] in real-time. Runs in a daemon thread."""
    while True:
        try:
            ev = events_q.get(timeout=2)
        except queue.Empty:
            continue
        if ev is None:
            break
        t = ev.get("type", "")
        if t == "started":
            run["log"].append(f"▶ LangSmith run started ({ev.get('total')} cases)")
        elif t == "langsmith_sync":
            run["log"].append(f"  ⟳ {ev.get('message', 'Syncing dataset…')}")
        elif t == "langsmith_sync_done":
            run["log"].append("  ✓ Dataset synced")
        elif t == "langsmith_pipeline_start":
            run["log"].append(f"  ▶ Running {ev.get('pipeline')} pipeline via LangSmith…")
        elif t == "langsmith_pipeline_done":
            url = ev.get("experiment_url")
            elapsed = f" ({ev.get('elapsed_s')}s)" if ev.get("elapsed_s") else ""
            ready = " — experiment ready" if url else ""
            run["log"].append(f"  ✓ {ev.get('pipeline')} done{elapsed}{ready}")
            if url:
                run.setdefault("langsmith_links", {})[ev.get("pipeline", "pipeline")] = url
        elif t == "langsmith_pipeline_error":
            run["log"].append(f"  ✗ {ev.get('pipeline')} error: {ev.get('error', '')}")
        elif t == "complete":
            run["log"].append("✓ LangSmith run complete")


# Eval cases within a single run share one pipeline (and one orchestrator-
# override lock acquisition for the whole batch — see _run_one_eval_case's
# docstring), so concurrency is bounded by external API/LLM rate limits, not
# by that lock. Scale with selection size so small runs (2-3 cases) get full
# parallelism while large runs don't fire off dozens of simultaneous search/
# LLM calls at once. 4 is the sweet spot: enough to overlap research I/O
# without triggering provider rate limits that open the circuit breaker.
# 10 was tripping the OpenAI circuit mid-run — cases that started after
# the circuit opened got "all candidates skipped" crashes on direct_answer.
_MAX_EVAL_CASE_CONCURRENCY = 4


def _run_in_process_core(run: dict, case_dicts: list[dict], pipeline: str = "langgraph", run_id: str | None = None) -> list[dict]:
    """Run ONE pipeline locally for every case, graded against each case's
    pre-determined expected_criteria. Mutates run["progress"]/["log"]/["completed"].
    Does NOT touch run["status"] — the caller decides when to flip to "complete"/"stopped".
    Returns early (with partial results already collected) if run["stop_requested"]
    is set — in-flight cases in the current batch are allowed to finish (no
    hard cancellation), but no new batch is dispatched after a stop request."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.agent.tools import Tools
    from app.services.provider_health import reset_circuit_state

    # Reset the provider circuit breaker before each run. Stale trip state
    # from a prior run (or from live traffic bursts) would cause all
    # provider candidates to be skipped for cases that happen to call a
    # provider after the circuit opened, producing ok=False crashes that
    # look like product failures but are actually harness infrastructure
    # failures. The reset only affects the in-process state — it does not
    # affect any external load balancer or provider-side rate limits.
    reset_circuit_state()

    tools = Tools.from_settings()
    total = len(case_dicts)
    run["total"] = total
    max_workers = min(total, _MAX_EVAL_CASE_CONCURRENCY) or 1
    run["log"].append(
        f"▶ Started ({total} case{'s' if total != 1 else ''}, in-process, "
        f"pipeline={pipeline}, concurrency={max_workers})"
    )
    all_results: list[dict] = []
    progress_lock = threading.Lock()

    def _run_case(idx: int, case_dict: dict) -> dict:
        with progress_lock:
            run["log"].append(f"  [{idx + 1}/{total}] {case_dict['title']}…")
        result = _run_one_eval_case(case_dict, tools, pipeline=pipeline)
        with progress_lock:
            all_results.append(result)
            run["progress"].append(result)
            run["completed"] = len(all_results)
            if result.get("overall_status") == "harness_error":
                # judge_structural_agreement failed — the pipeline reported a
                # confident judge_score against an empty/missing answer. This is
                # a scoring/pipeline integrity defect, not a product-quality
                # finding (scoring_spec.md §1.9) — flag loudly so it isn't
                # silently averaged into pass/fail dashboards downstream.
                logger.warning(
                    "eval case %s (%s) is a harness_error: judge_score=%s with empty answer",
                    case_dict.get("id"), case_dict.get("title"), result.get("run", {}).get("judge_score"),
                )
                run["log"].append(
                    f"  ⚠ [{idx + 1}/{total}] {case_dict['title']} — HARNESS ERROR "
                    f"(judge_score={result.get('run', {}).get('judge_score')} but empty answer)"
                )
            else:
                run["log"].append(f"  [{idx + 1}/{total}] {case_dict['title']} — done.")
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_case, idx, case_dict): idx
            for idx, case_dict in enumerate(case_dicts)
            if not run.get("stop_requested")
        }
        for future in as_completed(futures):
            future.result()  # surface exceptions; _run_one_eval_case already catches its own
            # Checkpoint partial progress to DB after each case so that
            # the status endpoint's DB fallback can serve real data on any
            # page reload or tab switch (not just while _EVAL_RUNS is live).
            if run_id:
                with progress_lock:
                    current_progress = list(run["progress"])
                _checkpoint_eval_run(run_id, current_progress, pipeline)
            if run.get("stop_requested"):
                for pending in futures:
                    pending.cancel()
                break

    if run.get("stop_requested") and len(all_results) < total:
        run["log"].append(f"⏹ Stopped after {len(all_results)} of {total} case(s).")
    return all_results


def _run_langsmith_core(run: dict, run_id: str, case_dicts: list[dict]) -> dict | None:
    """Run LangGraph via LangSmith evaluate(). Drains events into run["log"].
    Returns the LangSmith summary dict, or None if LS is not configured."""
    from app.services.langsmith_evals import is_configured as ls_configured, run_eval as ls_run_eval
    if not ls_configured():
        run["log"].append("⚠ LangSmith not configured — skipping LangSmith experiment.")
        return None
    run["log"].append(f"▶ Syncing to LangSmith dataset and running experiments…")
    ls_events: queue.Queue = queue.Queue()
    drainer = threading.Thread(
        target=_drain_ls_events_to_run, args=(ls_events, run), daemon=True,
        name=f"eval-ls-drain-{run_id}",
    )
    drainer.start()
    try:
        ls_summary = ls_run_eval(run_id, case_dicts, ls_events)
    finally:
        ls_events.put(None)
        drainer.join(timeout=10)
    return ls_summary


def _run_eval_background(
    run_id: str, case_dicts: list[dict], mode: str = "in_process", pipeline: str = "langgraph"
) -> None:
    """Runs in a daemon thread. Writes progress directly to the run dict.

    mode values:
      in_process — run ONE pipeline locally, graded against each case's
                   pre-determined expected_criteria; full per-case data stored in DB.
      langsmith  — run via langsmith.evaluate(); per-case data lives in LangSmith
                   (LangGraph-only).
      both       — run in-process first (local per-case data, single `pipeline`), then
                   run LangSmith experiments in the same thread (adds LS experiment
                   links to the envelope). Takes in_process_time + langsmith_time total.
    """
    run = _EVAL_RUNS.get(run_id)
    if run is None:
        return

    try:
        if mode == "langsmith":
            # LangSmith-only: no local per-case data.
            run["total"] = len(case_dicts)
            ls_summary = _run_langsmith_core(run, run_id, case_dicts)
            envelope = _make_result_envelope("langsmith", [], ls_summary, pipeline=pipeline)
            run["results"] = envelope
            run["status"] = "complete"
            run["completed_at"] = time.time()
            _persist_eval_run(run_id, envelope, "complete")

        elif mode == "both":
            # Phase 1 — in-process (full local per-case data)
            all_results = _run_in_process_core(run, case_dicts, pipeline=pipeline, run_id=run_id)
            if run.get("stop_requested"):
                envelope = _make_result_envelope("in_process", all_results, None, pipeline=pipeline)
                run["results"] = envelope
                run["status"] = "stopped"
                run["completed_at"] = time.time()
                _persist_eval_run(run_id, envelope, "stopped")
                return
            run["log"].append("✓ In-process eval done — running LangSmith experiments…")
            # Phase 2 — LangSmith (experiment tracking; re-runs pipelines via LS)
            ls_summary = _run_langsmith_core(run, run_id, case_dicts)
            envelope = _make_result_envelope("both", all_results, ls_summary, pipeline=pipeline)
            run["results"] = envelope
            run["status"] = "complete"
            run["completed_at"] = time.time()
            run["log"].append("✓ Run complete (local + LangSmith)")
            _persist_eval_run(run_id, envelope, "complete")

        else:
            # in_process (default)
            all_results = _run_in_process_core(run, case_dicts, pipeline=pipeline, run_id=run_id)
            if run.get("stop_requested"):
                envelope = _make_result_envelope("in_process", all_results, None, pipeline=pipeline)
                run["results"] = envelope
                run["status"] = "stopped"
                run["completed_at"] = time.time()
                _persist_eval_run(run_id, envelope, "stopped")
                return
            envelope = _make_result_envelope("in_process", all_results, None, pipeline=pipeline)
            run["results"] = envelope
            run["status"] = "complete"
            run["completed_at"] = time.time()
            run["log"].append("✓ Run complete")
            _persist_eval_run(run_id, envelope, "complete")

    except Exception as exc:
        import traceback
        err = traceback.format_exc()
        run["error"] = err
        run["status"] = "error"
        run["completed_at"] = time.time()
        run["log"].append(f"✗ Error: {str(exc)[:200]}")
        _persist_eval_run(run_id, _make_result_envelope("error", [], None), "error", error=err)


def _checkpoint_eval_run(run_id: str, partial_results: list[dict], pipeline: str) -> None:
    """Write partial per-case results to the DB after each case completes.

    This makes the status endpoint's DB fallback useful while a run is still
    in-flight — any page reload or tab switch can resume the progress display
    by reading from the DB rather than requiring the in-process _EVAL_RUNS
    dict to be reachable on the same request.  Status and completed_at are
    intentionally NOT updated; _persist_eval_run handles those on completion.
    """
    try:
        from app.db.models import EvalRun, SessionLocal
        envelope = _make_result_envelope("in_process", partial_results, None, pipeline=pipeline)
        db = SessionLocal()
        try:
            row = db.query(EvalRun).filter(EvalRun.id == run_id).first()
            if row:
                row.results_json = json.dumps(envelope, default=str)
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Could not checkpoint eval run %s to DB: %s", run_id, exc)


def _persist_eval_run(run_id: str, results: dict, status: str, error: str | None = None) -> None:
    try:
        from app.db.models import EvalRun, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(EvalRun).filter(EvalRun.id == run_id).first()
            if row:
                row.status = status
                row.results_json = json.dumps(results, default=str)
                row.error = error
                row.completed_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Could not persist eval run %s to DB: %s", run_id, exc)


class EvalRunRequest(BaseModel):
    case_ids: list[int] | None = None  # None = all cases
    mode: str = "in_process"          # in_process | langsmith | both
    pipeline: str = "langgraph"       # retained for old clients; always normalized to langgraph


@router.post("/runs", status_code=202)
def start_eval_run(body: EvalRunRequest = Body(default=None), admin: AdminPrincipal = RequireAdmin) -> dict:
    """Start a general eval run over selected (or all) cases against ONE pipeline,
    graded against each case's pre-determined expected_criteria (ground truth).

    pipeline=langgraph  Run the LangGraph pipeline (default and only runtime).
    mode=in_process      Run locally; full per-case data stored in DB (default).
    mode=langsmith       Run via LangSmith evaluate(); per-case data in LangSmith.
    mode=both            In-process first then LangSmith; double runtime, both datasets.
    """
    from app.db.models import EvalCase, EvalRun, SessionLocal

    pipeline = "langgraph"

    db = SessionLocal()
    try:
        q = db.query(EvalCase).filter(EvalCase.is_active.is_(True))
        if body and body.case_ids:
            q = q.filter(EvalCase.id.in_(body.case_ids))
        cases = q.order_by(EvalCase.created_at).all()
        if not cases:
            raise HTTPException(status_code=422, detail="No active eval cases found (create some first).")
        case_dicts = [_case_out(c) for c in cases]

        run_id = f"evalrun_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        row = EvalRun(
            id=run_id,
            status="running",
            started_by=admin.user_id,
            case_ids_json=json.dumps([c["id"] for c in case_dicts]),
            started_at=now,
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    mode = (body.mode if body and body.mode in ("in_process", "langsmith", "both") else "in_process")

    run = _make_eval_run(run_id)
    run["mode"] = mode
    run["pipeline"] = pipeline
    with _EVAL_RUNS_LOCK:
        _EVAL_RUNS[run_id] = run

    thread = threading.Thread(
        target=_run_eval_background,
        args=(run_id, case_dicts, mode, pipeline),
        daemon=True,
        name=f"eval-run-{run_id}",
    )
    thread.start()

    return {"run_id": run_id, "status": "running", "case_count": len(case_dicts), "mode": mode, "pipeline": pipeline}


@router.get("/runs")
def list_eval_runs(
    limit: int = 11,
    offset: int = 0,
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """List eval runs from DB (newest first).

    Supports pagination via limit/offset.  Callers typically request limit=11
    to fetch one extra row and detect whether a 'load more' page exists, then
    display only the first 10.
    """
    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        q = db.query(EvalRun).order_by(EvalRun.started_at.desc())
        total: int = q.count()
        rows = q.offset(offset).limit(min(limit, 50)).all()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "runs": [
                {
                    "run_id": r.id,
                    "status": r.status,
                    "started_by": r.started_by,
                    "case_count": len(json.loads(r.case_ids_json or "[]")),
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "error": r.error,
                    "live": r.id in _EVAL_RUNS,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.get("/runs/{run_id}/status")
def get_eval_run_status(run_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Poll for live progress and final results. Checks in-process memory, then DB.

    Always returns a consistent shape:
      status:    running | complete | stopped | error
      total:     total case count (null if not yet known)
      completed: number of cases finished so far
      progress:  list of per-case result dicts (grows as cases finish)
      log:       human-readable progress lines (live-only; empty from DB)
      results:   EvalRunResult envelope {mode, pipeline, cases, ...} — always
                 present even for in-flight runs (cases=[] until first case done)
    """
    with _EVAL_RUNS_LOCK:
        run = _EVAL_RUNS.get(run_id)
    if run:
        pipeline = run.get("pipeline", "langgraph")
        progress_snapshot = list(run.get("progress", []))
        # Always build a results envelope so the frontend has a consistent
        # shape regardless of how many cases have finished.  For a completed
        # run run.get("results") is the final envelope; for an in-flight run
        # it's None, so we build a partial one from the current progress.
        results = run.get("results") or _make_result_envelope(
            "in_process", progress_snapshot, None, pipeline=pipeline
        )
        return {
            "run_id": run_id,
            "status": run["status"],
            "mode": run.get("mode", "in_process"),
            "pipeline": pipeline,
            "total": run.get("total"),
            "completed": run.get("completed", 0),
            "progress": progress_snapshot,
            "log": list(run.get("log", [])),
            "results": results,
            "langsmith_links": run.get("langsmith_links"),
            "error": run.get("error"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
        }
    # Fall through to DB — handles historical runs AND runs whose process died
    # (server restart). The checkpoint writes keep results_json current while
    # the run is in-flight so this path returns real partial data, not just [].
    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(EvalRun).filter(EvalRun.id == run_id).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Eval run {run_id!r} not found.")
        raw = json.loads(row.results_json or "null")
        if isinstance(raw, list):
            results = _make_result_envelope("in_process", raw, None)
        elif isinstance(raw, dict):
            results = raw
        else:
            results = _make_result_envelope("in_process", [], None)
        progress = results.get("cases", []) if isinstance(results, dict) else []
        return {
            "run_id": run_id,
            "status": row.status,
            "total": len(progress) or None,
            "completed": len(progress),
            "progress": progress,
            "log": [],
            "results": results,
            "langsmith_links": None,
            "error": row.error,
            "started_at": row.started_at.timestamp() if row.started_at else None,
            "completed_at": row.completed_at.timestamp() if row.completed_at else None,
        }
    finally:
        db.close()


@router.get("/runs/{run_id}/result")
def get_eval_run_result(run_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Return full results for a completed run; checks memory then DB."""
    # Check in-process first
    with _EVAL_RUNS_LOCK:
        run = _EVAL_RUNS.get(run_id)
    if run:
        if run["status"] == "running":
            return {"status": "running", "run_id": run_id}
        if run["status"] == "error":
            return {"status": "error", "run_id": run_id, "error": run.get("error", "")}
        return {"status": "complete", "run_id": run_id, "results": run["results"]}

    # Fall through to DB for historical runs
    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(EvalRun).filter(EvalRun.id == run_id).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Eval run {run_id!r} not found.")
        raw = json.loads(row.results_json or "null")
        # Normalise: old runs stored a raw list; new runs store the envelope dict.
        if isinstance(raw, list):
            results = _make_result_envelope("in_process", raw, None)
        elif isinstance(raw, dict):
            results = raw
        else:
            results = _make_result_envelope("in_process", [], None)
        return {"status": row.status, "run_id": run_id, "results": results, "error": row.error}
    finally:
        db.close()


def _get_run_for_export(run_id: str) -> tuple[str, dict, list[int]]:
    """Return (status, results_envelope, case_ids) for a run, checking
    in-process memory then DB. Raises HTTPException(404) if not found."""
    with _EVAL_RUNS_LOCK:
        run = _EVAL_RUNS.get(run_id)
    if run:
        results = run.get("results") or _make_result_envelope("in_process", list(run.get("progress", [])), None)
        case_ids = [c["case_id"] for c in results.get("cases", [])] if isinstance(results, dict) else []
        return run["status"], results, case_ids

    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(EvalRun).filter(EvalRun.id == run_id).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Eval run {run_id!r} not found.")
        raw = json.loads(row.results_json or "null")
        if isinstance(raw, list):
            results = _make_result_envelope("in_process", raw, None)
        elif isinstance(raw, dict):
            results = raw
        else:
            results = _make_result_envelope("in_process", [], None)
        case_ids = json.loads(row.case_ids_json or "[]")
        return row.status, results, case_ids
    finally:
        db.close()


@router.get("/runs/{run_id}/dashboard")
def get_eval_run_dashboard(run_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    """scoring_spec.md §3/§6 — axis x tier rollup + Harness Integrity panel
    for a run. Check `integrity.ok` before trusting `table` at all (the spec's
    explicit sequencing requirement) — a harness_error or canary drift means
    something in the scoring pipeline itself is suspect that run."""
    _, results, _ = _get_run_for_export(run_id)
    cases = results.get("cases", []) if isinstance(results, dict) else []
    return compute_dashboard(cases)


@router.get("/runs/{run_id}/export")
def export_eval_run(
    run_id: str, format: str = "json", admin: AdminPrincipal = RequireAdmin
) -> Response:
    """Download a run's full results for offline analysis.

    format=json (default) — the full results envelope, pretty-printed.
    format=csv — one row per case with the key scored fields flattened out
    (criteria/benchmark detail is summarized, not fully expanded, to keep
    the CSV tabular).
    """
    status, results, _ = _get_run_for_export(run_id)
    cases = results.get("cases", []) if isinstance(results, dict) else []

    if format == "csv":
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "case_id", "title", "pipeline", "ok", "answer_length", "evidence_count",
            "claim_count", "independent_source_count", "latency_ms", "criteria_score",
            "criteria_passed_count", "criteria_failed_count", "overall_structural_pass",
            "overall_benchmark_pass", "error",
        ])
        for c in cases:
            run = c.get("run", {})
            criteria = run.get("criteria") or {}
            writer.writerow([
                c.get("case_id"), c.get("title"), c.get("pipeline"), run.get("ok"),
                run.get("answer_length"), run.get("evidence_count"), run.get("claim_count"),
                run.get("independent_source_count"), run.get("latency_ms"), criteria.get("score"),
                len(criteria.get("passed") or []), len(criteria.get("failed") or []),
                c.get("overall_structural_pass"), c.get("overall_benchmark_pass"), run.get("error"),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'},
        )

    payload = {"run_id": run_id, "status": status, "results": results}
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
    )


@router.delete("/runs/{run_id}", status_code=204)
def delete_eval_run(run_id: str, admin: AdminPrincipal = RequireAdmin) -> None:
    """Permanently delete a single eval run (DB row + in-process state if live).

    If the run is still actively running, this requests it to stop
    (best-effort — in-flight LLM/search calls aren't forcibly killed, just
    no new cases get dispatched after the current batch) and deletes
    anyway, rather than blocking. Safe to do: _persist_eval_run no-ops if
    its row no longer exists by the time the background thread eventually
    finishes or errors (`if row:` guard) — it never recreates a deleted
    row. Previously this raised 409 and left genuinely-stuck runs (e.g. an
    in-memory entry orphaned by a server restart that somehow still looked
    "running", or a thread truly hung on a call with no timeout)
    permanently undeletable from the UI, with no way to force-stop them.
    """
    with _EVAL_RUNS_LOCK:
        run = _EVAL_RUNS.get(run_id)
        if run:
            run["stop_requested"] = True
        _EVAL_RUNS.pop(run_id, None)

    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(EvalRun).filter(EvalRun.id == run_id).first()
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


class EvalRunCleanupRequest(BaseModel):
    keep_latest: int = Field(default=20, ge=0)


@router.post("/runs/cleanup")
def cleanup_eval_runs(
    body: EvalRunCleanupRequest = Body(default=None), admin: AdminPrincipal = RequireAdmin
) -> dict:
    """Delete all but the `keep_latest` most recent eval runs (default 20).
    Never deletes a run that's currently in progress."""
    keep_latest = body.keep_latest if body else 20
    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(EvalRun).order_by(EvalRun.started_at.desc()).all()
        with _EVAL_RUNS_LOCK:
            running_ids = {rid for rid, r in _EVAL_RUNS.items() if r["status"] == "running"}
        to_delete = [r for r in rows[keep_latest:] if r.id not in running_ids]
        for row in to_delete:
            db.delete(row)
        db.commit()
        return {"deleted": len(to_delete), "kept": len(rows) - len(to_delete)}
    finally:
        db.close()


@router.post("/runs/{run_id}/rerun", status_code=202)
def rerun_eval_run(run_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Start a new run with the same case selection, pipeline, and mode as
    an existing run — one click instead of re-selecting cases manually."""
    _, results, case_ids = _get_run_for_export(run_id)
    pipeline = results.get("pipeline", "langgraph") if isinstance(results, dict) else "langgraph"
    mode = results.get("mode", "in_process") if isinstance(results, dict) else "in_process"
    if mode not in ("in_process", "langsmith", "both"):
        mode = "in_process"
    body = EvalRunRequest(case_ids=case_ids or None, mode=mode, pipeline=pipeline)
    return start_eval_run(body=body, admin=admin)


@router.post("/runs/{run_id}/stop", status_code=200)
def stop_eval_run(run_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Request early termination of a running eval.

    Sets stop_requested=True on the in-process run dict. The background thread
    checks this flag between cases and exits cleanly, persisting partial results
    with status="stopped". If the run is already complete or not found, this is a no-op.
    """
    with _EVAL_RUNS_LOCK:
        run = _EVAL_RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Eval run {run_id!r} not found or already completed.")
    if run["status"] != "running":
        return {"run_id": run_id, "status": run["status"], "message": "Run is not active."}
    run["stop_requested"] = True
    run["log"].append("⏹ Stop requested — will halt after current case finishes.")
    return {"run_id": run_id, "status": "stopping", "message": "Stop requested."}
