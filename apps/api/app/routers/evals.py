"""Admin eval endpoints.

Parity routes (legacy vs LangGraph golden-set comparison):
  POST   /admin/evals/parity/run              Start parity run (background)
  GET    /admin/evals/parity/runs             List recent in-process parity runs
  GET    /admin/evals/parity/{run_id}/status  Poll for live progress + final report
  GET    /admin/evals/parity/{run_id}/result  Full ParityReport (alias for /status)
  GET    /admin/evals/parity/orchestrator     Current effective orchestrator
  POST   /admin/evals/parity/promote          Flip to langgraph (requires passing run)
  DELETE /admin/evals/parity/promote          Revert to env/config default

Eval case CRUD:
  GET    /admin/evals/cases                   List all cases
  POST   /admin/evals/cases                   Create case
  GET    /admin/evals/cases/{id}              Get case
  PUT    /admin/evals/cases/{id}              Update case
  DELETE /admin/evals/cases/{id}              Delete case

General eval runs (both pipelines, structural + criteria scoring):
  POST   /admin/evals/runs                    Start a run over selected (or all) cases
  GET    /admin/evals/runs                    List recent runs
  GET    /admin/evals/runs/{run_id}/stream    SSE per-case progress
  GET    /admin/evals/runs/{run_id}/result    Full results
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse  # still used by eval-runs stream
from pydantic import BaseModel, Field

from app.auth import AdminPrincipal, RequireAdmin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/evals", tags=["admin"])

# ---------------------------------------------------------------------------
# In-process run registry
# ---------------------------------------------------------------------------

_RUNS: dict[str, dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()
_MAX_STORED_RUNS = 20  # keep at most N completed runs in memory


def _make_run(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "running",    # running | complete | error
        "total": None,          # set once golden set is loaded
        "completed": 0,         # incremented after each case finishes
        "progress": [],         # list of per-case result dicts (poll to watch growth)
        "log": [],              # human-readable progress lines for the UI log panel
        "report": None,
        "error": None,
        "started_at": time.time(),
        "completed_at": None,
    }


def _get_run(run_id: str) -> dict[str, Any]:
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Parity run {run_id!r} not found.")
    return run


def _evict_old_runs() -> None:
    """Keep _RUNS from growing unbounded; evict the oldest completed runs."""
    with _RUNS_LOCK:
        completed = sorted(
            [(k, v) for k, v in _RUNS.items() if v["status"] != "running"],
            key=lambda kv: kv[1].get("started_at", 0),
        )
        while len(_RUNS) > _MAX_STORED_RUNS and completed:
            oldest_id, _ = completed.pop(0)
            del _RUNS[oldest_id]


# ---------------------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------------------

def _load_parity_comparator_module():
    """Load evals/run_parity_comparator.py directly from its file path.

    We use importlib.util rather than a bare `from evals.xxx import ...` because
    the FastAPI process may not have apps/api on sys.path and the evals/ directory
    has no __init__.py, making it an implicit namespace package that can be shadowed
    by other installed packages.  Loading by absolute path is fully deterministic.
    """
    import importlib.util as ilu
    api_root = Path(__file__).resolve().parents[2]  # apps/api
    mod_path = api_root / "evals" / "run_parity_comparator.py"
    if not mod_path.exists():
        raise ImportError(f"run_parity_comparator.py not found at {mod_path}")
    spec = ilu.spec_from_file_location("_parity_runner", mod_path)
    mod = ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_parity_background(run_id: str, case_ids: list[str] | None) -> None:
    """Runs in a daemon thread. Writes progress directly to the run dict so the
    polling endpoint can serve a consistent snapshot without an SSE connection."""
    run = _get_run(run_id)

    def _log(msg: str) -> None:
        run["log"].append(msg)

    try:
        from app.services.agent.langgraph_runtime.comparators import (
            aggregate_parity_results,
            compare_pipeline_results,
        )
        from app.services.agent.tools import Tools
        _parity = _load_parity_comparator_module()
        _load_golden_set = _parity._load_golden_set
        _run_legacy = _parity._run_legacy
        _run_langgraph = _parity._run_langgraph

        tools = Tools.from_settings()
        golden_set = _load_golden_set(case_ids)
        total = len(golden_set)
        run["total"] = total

        _log(f"▶ Started ({total} cases)")

        per_case_results = []

        for idx, entry in enumerate(golden_set):
            case_id = entry["id"]
            _log(f"  [{idx + 1}/{total}] Running {case_id}…")

            t0 = time.perf_counter()
            legacy_result, legacy_err = _run_legacy(entry, tools)
            legacy_ms = int((time.perf_counter() - t0) * 1000)

            t1 = time.perf_counter()
            langgraph_result, langgraph_err = _run_langgraph(entry, tools)
            langgraph_ms = int((time.perf_counter() - t1) * 1000)

            pr = compare_pipeline_results(
                case_id=case_id,
                legacy_result=legacy_result,
                langgraph_result=langgraph_result,
                legacy_error=legacy_err,
                langgraph_error=langgraph_err,
            )
            per_case_results.append(pr)

            result_dict = pr.to_dict()
            result_dict["legacy_ms"] = legacy_ms
            result_dict["langgraph_ms"] = langgraph_ms

            icon = "✓" if pr.overall_pass else "✗"
            _log(
                f"  {icon} {case_id} — ans={result_dict.get('answer_length_ratio', '–'):.2f}"
                f" evid={result_dict.get('evidence_count_ratio', '–'):.2f}"
                f" claims={result_dict.get('claim_count_ratio', '–'):.2f}"
            )

            # Append to progress list and bump counter atomically (CPython GIL).
            run["progress"].append(result_dict)
            run["completed"] = idx + 1

        report = aggregate_parity_results(per_case_results)
        report_dict = report.to_dict()

        run["report"] = report_dict
        run["status"] = "complete"
        run["completed_at"] = time.time()

        verdict = "✅ CUTOVER RECOMMENDED" if report.cutover_recommended else "❌ NOT READY"
        _log(f"{verdict} — {report.overall_pass}/{total} cases pass all gates")

        try:
            _save_report(run_id, report_dict)
        except Exception as save_exc:
            logger.warning("evals: could not save parity report to disk: %s", save_exc)

    except Exception as exc:
        import traceback
        err = traceback.format_exc()
        logger.exception("Parity run %s failed", run_id)
        run["error"] = err
        run["status"] = "error"
        run["completed_at"] = time.time()
        _log(f"❌ Error: {exc}")


def _save_report(run_id: str, report_dict: dict) -> None:
    results_dir = Path(__file__).resolve().parents[2] / "evals" / "parity_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{run_id}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report_dict, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/parity/run")
def start_parity_run(
    case_ids: list[str] | None = Body(default=None),
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """Start a background parity run. Returns run_id to poll for progress."""
    # Prevent running multiple concurrent full runs
    with _RUNS_LOCK:
        active = [r for r in _RUNS.values() if r["status"] == "running"]
    if active:
        return {
            "run_id": active[0]["run_id"],
            "status": "already_running",
            "message": "A parity run is already in progress.",
        }

    run_id = f"parity_{uuid.uuid4().hex[:12]}"
    run = _make_run(run_id)
    with _RUNS_LOCK:
        _RUNS[run_id] = run

    _evict_old_runs()

    thread = threading.Thread(
        target=_run_parity_background,
        args=(run_id, case_ids),
        daemon=True,
        name=f"parity-run-{run_id}",
    )
    thread.start()

    logger.info("Parity run %s started by admin %s", run_id, admin.user_id)
    return {"run_id": run_id, "status": "running"}


@router.get("/parity/runs")
def list_parity_runs(admin: AdminPrincipal = RequireAdmin) -> dict:
    """List recent in-process parity runs (newest first)."""
    with _RUNS_LOCK:
        runs = sorted(_RUNS.values(), key=lambda r: r["started_at"], reverse=True)
    return {
        "runs": [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "cutover_recommended": (
                    r["report"].get("cutover_recommended") if r["report"] else None
                ),
                "overall_pass": r["report"].get("overall_pass") if r["report"] else None,
                "total_cases": r["report"].get("total_cases") if r["report"] else None,
            }
            for r in runs
        ]
    }


@router.get("/parity/{run_id}/status")
def get_parity_status(
    run_id: str,
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """Poll for live run progress and final report.

    Returns a consistent snapshot regardless of whether the run is still in
    progress or complete.  The UI polls this every 3 s while status == 'running'.

    Shape:
      {
        run_id, status, total, completed,
        progress: [ParityCaseResult, ...],   # grows as cases finish
        log: [str, ...],                     # human-readable progress lines
        report: ParityReport | null,         # populated on completion
        error: str | null,
        started_at, completed_at
      }
    """
    run = _get_run(run_id)
    return {
        "run_id": run_id,
        "status": run["status"],
        "total": run["total"],
        "completed": run["completed"],
        "progress": list(run["progress"]),   # snapshot copy
        "log": list(run["log"]),
        "report": run["report"],
        "error": run["error"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
    }


@router.get("/parity/{run_id}/result")
def get_parity_result(
    run_id: str,
    admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """Alias for /status — kept for backward compatibility."""
    return get_parity_status(run_id, admin)


@router.post("/parity/promote")
def promote_langgraph(admin: AdminPrincipal = RequireAdmin) -> dict:
    """Flip the in-process orchestrator to langgraph after a passing parity run.

    Validates that at least one completed parity run recommends cutover before
    allowing promotion.  The override lasts for the lifetime of this process;
    set FRONEI_ORCHESTRATOR=langgraph in the deployment environment to persist
    it across restarts.
    """
    # Require a passing run before promoting
    with _RUNS_LOCK:
        passing_runs = [
            r for r in _RUNS.values()
            if r["status"] == "complete"
            and r["report"]
            and r["report"].get("cutover_recommended")
        ]

    if not passing_runs:
        raise HTTPException(
            status_code=409,
            detail=(
                "No completed parity run recommends cutover. "
                "Run a parity comparison first and ensure all gates pass."
            ),
        )

    from app.services.agent.langgraph_runtime.runtime import set_orchestrator_override
    set_orchestrator_override("langgraph")

    logger.info(
        "Orchestrator promoted to langgraph by admin %s (process-lifetime override).",
        admin.user_id,
    )
    return {
        "effective_orchestrator": "langgraph",
        "promoted_by": admin.user_id,
        "note": (
            "LangGraph is now active for this process. "
            "Set FRONEI_ORCHESTRATOR=langgraph in your deployment environment "
            "to persist this across restarts."
        ),
    }


@router.delete("/parity/promote")
def revert_orchestrator(admin: AdminPrincipal = RequireAdmin) -> dict:
    """Revert the in-process override; legacy or FRONEI_ORCHESTRATOR env var takes effect."""
    from app.services.agent.langgraph_runtime.runtime import (
        clear_orchestrator_override,
        configured_orchestrator,
    )
    clear_orchestrator_override()
    effective = configured_orchestrator()
    logger.info(
        "Orchestrator override cleared by admin %s; effective orchestrator: %s",
        admin.user_id,
        effective,
    )
    return {
        "effective_orchestrator": effective,
        "reverted_by": admin.user_id,
    }


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


@router.get("/parity/orchestrator")
def get_orchestrator_status(admin: AdminPrincipal = RequireAdmin) -> dict:
    """Return the current effective orchestrator and whether an override is active."""
    from app.services.agent.langgraph_runtime import runtime as lg_runtime
    from app.config import get_settings
    settings = get_settings()
    return {
        "effective_orchestrator": lg_runtime.configured_orchestrator(),
        "override_active": lg_runtime._RUNTIME_ORCHESTRATOR_OVERRIDE is not None,
        "override_value": lg_runtime._RUNTIME_ORCHESTRATOR_OVERRIDE,
        "env_default": settings.fronei_orchestrator,
    }


# ===========================================================================
# Eval case CRUD
# ===========================================================================

class EvalCaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1)
    category: str | None = Field(default=None, max_length=128)
    # List of natural-language criteria strings, e.g. ["mentions official SLA", "cites practitioner data"]
    expected_criteria: list[str] = Field(default_factory=list)
    expected_primary_role: str | None = Field(default=None, max_length=64)
    min_independent_sources: int | None = Field(default=None, ge=1)
    notes: str | None = None


class EvalCaseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    query: str | None = Field(default=None, min_length=1)
    category: str | None = None
    expected_criteria: list[str] | None = None
    expected_primary_role: str | None = None
    min_independent_sources: int | None = Field(default=None, ge=1)
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
        "notes": c.notes,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


@router.get("/cases")
def list_eval_cases(admin: AdminPrincipal = RequireAdmin) -> dict:
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        cases = db.query(EvalCase).order_by(EvalCase.created_at.desc()).all()
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
    from app.db.models import EvalCase, SessionLocal
    db = SessionLocal()
    try:
        case = db.query(EvalCase).filter(EvalCase.id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail=f"Eval case {case_id} not found.")
        db.delete(case)
        db.commit()
    finally:
        db.close()


# ===========================================================================
# General eval runs (both pipelines + criteria scoring)
# ===========================================================================

# In-process registry for general eval runs (same pattern as parity runs)
_EVAL_RUNS: dict[str, dict[str, Any]] = {}
_EVAL_RUNS_LOCK = threading.Lock()


def _make_eval_run(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "running",
        "events": queue.Queue(),
        "results": [],
        "error": None,
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


def _run_one_eval_case(case_dict: dict, tools) -> dict[str, Any]:
    """Run a single eval case through both pipelines and score it."""
    from app.services.agent.models import TurnRequest
    import traceback as tb

    query = case_dict["query"]
    criteria = case_dict.get("expected_criteria") or []

    def _run_pipeline(fn_name: str):
        request = TurnRequest(message=query, research_level="regular", quality_mode="standard", output_format="chat")
        t0 = time.perf_counter()
        err = None
        result = None
        try:
            if fn_name == "legacy":
                from app.services.agent.research_lead import lead_research_loop
                result = lead_research_loop(request, tools, progress=None)
            else:
                from app.services.agent.langgraph_runtime.runtime import run_langgraph_research
                result = run_langgraph_research(request, tools, progress=None)
        except Exception:
            err = tb.format_exc()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return result, err, elapsed_ms

    legacy_result, legacy_err, legacy_ms = _run_pipeline("legacy")
    langgraph_result, langgraph_err, langgraph_ms = _run_pipeline("langgraph")

    def _extract(result, err) -> dict:
        if err or result is None:
            return {"ok": False, "error": (err or "")[:500], "answer": "", "answer_length": 0,
                    "evidence_count": 0, "claim_count": 0, "judge_score": None}
        response = result.get("response")
        answer = response.text if hasattr(response, "text") else str(response or "")
        evidence = result.get("evidence")
        feedback = result.get("feedback")
        return {
            "ok": True,
            "error": None,
            "answer": answer[:2000],
            "answer_length": len(answer),
            "evidence_count": len(evidence.items) if evidence and hasattr(evidence, "items") else 0,
            "claim_count": len(evidence.claims) if evidence and hasattr(evidence, "claims") else 0,
            "judge_score": getattr(feedback, "final_score", None) if feedback else None,
        }

    leg = _extract(legacy_result, legacy_err)
    lg = _extract(langgraph_result, langgraph_err)

    # Structural checks
    structural = {
        "legacy_ok": leg["ok"],
        "langgraph_ok": lg["ok"],
        "legacy_non_empty_answer": leg["answer_length"] > 0,
        "langgraph_non_empty_answer": lg["answer_length"] > 0,
        "legacy_has_evidence": leg["evidence_count"] > 0,
        "langgraph_has_evidence": lg["evidence_count"] > 0,
    }

    # Criteria scoring (score each pipeline independently)
    legacy_criteria = _score_criteria(query, leg["answer"], criteria) if criteria and leg["ok"] else None
    langgraph_criteria = _score_criteria(query, lg["answer"], criteria) if criteria and lg["ok"] else None

    return {
        "case_id": case_dict["id"],
        "title": case_dict["title"],
        "query": query,
        "legacy": {**leg, "latency_ms": legacy_ms, "criteria": legacy_criteria},
        "langgraph": {**lg, "latency_ms": langgraph_ms, "criteria": langgraph_criteria},
        "structural": structural,
        "overall_structural_pass": all(structural.values()),
    }


def _make_result_envelope(mode: str, cases: list, langsmith_summary: dict | None) -> dict:
    """Consistent result envelope stored in memory and DB regardless of eval mode.

    Shape:
      {
        "mode": "langsmith" | "in_process",
        "cases": [EvalCaseRunResult, ...],  # empty for LangSmith runs (LangSmith is source of truth)
        "langsmith": { ... } | null         # LangSmith experiment summary, null for in-process
      }

    This guarantees /runs/{run_id}/result always returns the same contract; the
    caller need not branch on mode to read per-case rows vs. a summary dict.
    """
    return {"mode": mode, "cases": cases, "langsmith": langsmith_summary}


def _run_eval_background(run_id: str, case_dicts: list[dict]) -> None:
    run = _EVAL_RUNS.get(run_id)
    if run is None:
        return
    events: queue.Queue = run["events"]

    try:
        from app.services.langsmith_evals import is_configured as ls_configured, run_eval as ls_run_eval

        if ls_configured():
            # --- LangSmith path ---
            # Per-case rows are not available in-process; LangSmith holds them.
            # Store an empty cases list and put the experiment summary in "langsmith".
            events.put({"type": "started", "total": len(case_dicts), "run_id": run_id, "mode": "langsmith"})
            ls_summary = ls_run_eval(run_id, case_dicts, events)
            envelope = _make_result_envelope("langsmith", [], ls_summary)
            run["results"] = envelope
            run["status"] = "complete"
            run["completed_at"] = time.time()
            _persist_eval_run(run_id, envelope, "complete")
            events.put({"type": "complete", "results": envelope, "run_id": run_id})

        else:
            # --- In-process fallback path ---
            from app.services.agent.tools import Tools
            tools = Tools.from_settings()
            total = len(case_dicts)
            events.put({"type": "started", "total": total, "run_id": run_id, "mode": "in_process"})

            all_results = []
            for idx, case_dict in enumerate(case_dicts):
                events.put({"type": "case_start", "case_id": case_dict["id"], "title": case_dict["title"],
                            "index": idx, "total": total})
                result = _run_one_eval_case(case_dict, tools)
                all_results.append(result)
                events.put({"type": "case_result", "case_id": case_dict["id"], "index": idx,
                            "total": total, "result": result})

            envelope = _make_result_envelope("in_process", all_results, None)
            run["results"] = envelope
            run["status"] = "complete"
            run["completed_at"] = time.time()
            _persist_eval_run(run_id, envelope, "complete")
            events.put({"type": "complete", "results": envelope, "run_id": run_id})

    except Exception as exc:
        import traceback
        err = traceback.format_exc()
        run["error"] = err
        run["status"] = "error"
        run["completed_at"] = time.time()
        _persist_eval_run(run_id, _make_result_envelope("error", [], None), "error", error=err)
        events.put({"type": "error", "error": str(exc), "run_id": run_id})
    finally:
        events.put(None)


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


@router.post("/runs", status_code=202)
def start_eval_run(body: EvalRunRequest = Body(default=None), admin: AdminPrincipal = RequireAdmin) -> dict:
    """Start a general eval run over selected (or all) cases — both pipelines."""
    from app.db.models import EvalCase, EvalRun, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(EvalCase)
        if body and body.case_ids:
            q = q.filter(EvalCase.id.in_(body.case_ids))
        cases = q.order_by(EvalCase.created_at).all()
        if not cases:
            raise HTTPException(status_code=422, detail="No eval cases found (create some first).")
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

    run = _make_eval_run(run_id)
    with _EVAL_RUNS_LOCK:
        _EVAL_RUNS[run_id] = run

    thread = threading.Thread(
        target=_run_eval_background,
        args=(run_id, case_dicts),
        daemon=True,
        name=f"eval-run-{run_id}",
    )
    thread.start()

    return {"run_id": run_id, "status": "running", "case_count": len(case_dicts)}


@router.get("/runs")
def list_eval_runs(admin: AdminPrincipal = RequireAdmin) -> dict:
    """List recent eval runs from DB (newest first)."""
    from app.db.models import EvalRun, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(EvalRun).order_by(EvalRun.started_at.desc()).limit(20).all()
        return {
            "runs": [
                {
                    "run_id": r.id,
                    "status": r.status,
                    "started_by": r.started_by,
                    "case_count": len(json.loads(r.case_ids_json or "[]")),
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "error": r.error,
                    # In-progress run: pull live stats from memory
                    "live": r.id in _EVAL_RUNS,
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@router.get("/runs/{run_id}/stream")
def stream_eval_run(run_id: str, admin: AdminPrincipal = RequireAdmin) -> StreamingResponse:
    run = _get_eval_run(run_id)
    events: queue.Queue = run["events"]

    def generate():
        yield "retry: 3000\n\n"
        while True:
            try:
                event = events.get(timeout=10)
            except queue.Empty:
                yield ": heartbeat\n\n"  # SSE comment keeps proxy alive without firing client events
                continue
            if event is None:
                yield "event: close\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
