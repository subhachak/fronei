"""LangSmith integration: dataset sync, tracing setup, and eval runner.

When LANGSMITH_API_KEY is set this module:
  1. Configures LANGCHAIN_TRACING_V2 env vars so LangGraph traces automatically.
  2. Keeps a LangSmith Dataset in sync with the eval_cases DB table.
  3. Runs both pipelines through langsmith.evaluate(), replacing the ad-hoc
     in-process criteria scorer with LangSmith's experiment tracking.

When the key is absent every public function either no-ops or raises
LangSmithNotConfigured so callers can fall back gracefully.
"""
from __future__ import annotations

import logging
import os
import queue as _queue
import time
import traceback
from typing import Any

log = logging.getLogger(__name__)

DATASET_NAME = "fronei-eval-cases"


class LangSmithNotConfigured(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def configure_tracing() -> bool:
    """Set LangChain env vars from settings. Returns True if tracing is on."""
    from app.config import get_settings
    s = get_settings()

    if not s.langsmith_api_key:
        return False

    os.environ["LANGSMITH_API_KEY"] = s.langsmith_api_key
    os.environ["LANGCHAIN_API_KEY"] = s.langsmith_api_key  # alias used by older SDK
    os.environ["LANGCHAIN_PROJECT"] = s.langchain_project

    # Tracing requires explicit opt-in via LANGCHAIN_TRACING_V2=true.
    # A key alone does NOT enable tracing — this prevents accidental upload of
    # prompts, retrieved evidence, and user queries in production environments.
    tracing_on = bool(s.langchain_tracing_v2)
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if tracing_on else "false"

    log.info(
        "LangSmith configured — project=%r tracing=%s%s",
        s.langchain_project,
        "ON" if tracing_on else "OFF",
        "" if tracing_on else " (set LANGCHAIN_TRACING_V2=true to enable)",
    )
    return tracing_on


def is_configured() -> bool:
    from app.config import get_settings
    return bool(get_settings().langsmith_api_key)


def _get_client():
    if not is_configured():
        raise LangSmithNotConfigured("LANGSMITH_API_KEY is not set.")
    from langsmith import Client
    from app.config import get_settings
    return Client(api_key=get_settings().langsmith_api_key)


# ---------------------------------------------------------------------------
# Dataset sync
# ---------------------------------------------------------------------------

def sync_dataset(cases: list[dict]) -> str:
    """Upsert eval cases into the LangSmith dataset. Returns dataset ID."""
    client = _get_client()

    # Get or create dataset
    try:
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
    except Exception:
        dataset = client.create_dataset(
            DATASET_NAME,
            description=(
                "Fronei golden eval cases — seeded from eval_cases DB table. "
                "Each example has inputs.query and outputs.expected_criteria."
            ),
        )

    # Index existing examples by the stable integer case_id stored in metadata.
    # Using case_id (not title) means renames don't create duplicate examples.
    existing: dict[int, Any] = {}
    for ex in client.list_examples(dataset_id=dataset.id):
        cid = (ex.metadata or {}).get("case_id")
        if cid is not None:
            existing[int(cid)] = ex

    created = updated = 0
    for case in cases:
        case_id = case.get("id")
        inputs = {"query": case["query"]}
        outputs = {
            "expected_criteria": case.get("expected_criteria", []),
            "expected_primary_role": case.get("expected_primary_role"),
        }
        metadata = {
            "case_title": case["title"],
            "category": case.get("category"),
            "case_id": case_id,
        }

        if case_id is not None and case_id in existing:
            client.update_example(
                existing[case_id].id,
                inputs=inputs,
                outputs=outputs,
                metadata=metadata,
            )
            updated += 1
        else:
            client.create_example(
                inputs=inputs,
                outputs=outputs,
                dataset_id=dataset.id,
                metadata=metadata,
            )
            created += 1

    log.info("LangSmith dataset sync: %d created, %d updated", created, updated)
    return str(dataset.id)


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------

def _make_criteria_evaluator():
    """LangSmith evaluator that scores a pipeline answer against expected criteria."""
    def criteria_evaluator(outputs: dict, reference_outputs: dict) -> dict:
        answer = (outputs or {}).get("answer", "")
        criteria: list[str] = (reference_outputs or {}).get("expected_criteria", [])

        if not criteria or not answer:
            return {"key": "criteria_score", "score": None, "comment": "No criteria or empty answer."}

        # Fast LLM judge (reuse the existing scorer)
        from app.routers.evals import _score_criteria
        result = _score_criteria("", answer, criteria)
        return {
            "key": "criteria_score",
            "score": result.get("score"),
            "comment": result.get("explanation", ""),
        }

    return criteria_evaluator


def _make_role_evaluator():
    """Binary evaluator: does the answer foreground the expected primary evidence role?"""
    def role_evaluator(outputs: dict, reference_outputs: dict) -> dict:
        answer = (outputs or {}).get("answer", "")
        role = (reference_outputs or {}).get("expected_primary_role")
        if not role or not answer:
            return {"key": "primary_role_present", "score": None}

        role_keywords = {
            "official_policy": ["official", "policy", "uscis", "regulation", "statute", "rule"],
            "operational_reality": ["practitioners", "reported", "forum", "experience", "months"],
            "anecdotal_case": ["reported", "users", "patients", "forum", "community"],
            "expert_interpretation": ["analysts", "experts", "forecast", "estimate", "consensus"],
        }
        keywords = role_keywords.get(role, [])
        if not keywords:
            return {"key": "primary_role_present", "score": None}

        answer_lower = answer.lower()
        hits = sum(1 for kw in keywords if kw in answer_lower)
        score = min(1.0, hits / max(len(keywords) * 0.5, 1))
        return {"key": "primary_role_present", "score": round(score, 2)}

    return role_evaluator


# ---------------------------------------------------------------------------
# Pipeline runner (target for langsmith.evaluate)
# ---------------------------------------------------------------------------

def _make_pipeline_target(pipeline_name: str, tools):
    def target(inputs: dict) -> dict:
        from app.services.agent.models import TurnRequest
        query = inputs.get("query", "")
        request = TurnRequest(
            message=query,
            research_level="regular",
            quality_mode="standard",
            output_format="chat",
        )
        try:
            if pipeline_name == "legacy":
                from app.services.agent.research_lead import lead_research_loop
                result = lead_research_loop(request, tools, progress=None)
            else:
                from app.services.agent.langgraph_runtime.runtime import run_langgraph_research
                result = run_langgraph_research(request, tools, progress=None)

            response = result.get("response")
            answer = response.text if hasattr(response, "text") else str(response or "")
            evidence = result.get("evidence")
            return {
                "answer": answer,
                "answer_length": len(answer),
                "evidence_count": len(evidence.items) if evidence and hasattr(evidence, "items") else 0,
                "claim_count": len(evidence.claims) if evidence and hasattr(evidence, "claims") else 0,
            }
        except Exception:
            return {"answer": "", "error": traceback.format_exc()[:500]}

    return target


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------

def run_eval(
    run_id: str,
    cases: list[dict],
    events: _queue.Queue,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    """Run both pipelines via langsmith.evaluate(). Emits SSE-style events to queue.

    Returns a summary dict stored in EvalRun.results_json.
    """
    from langsmith import evaluate
    from app.services.agent.tools import Tools

    tools = Tools.from_settings()

    # Sync cases to LangSmith dataset first
    events.put({"type": "langsmith_sync", "message": "Syncing cases to LangSmith dataset…"})
    try:
        dataset_id = sync_dataset(cases)
        events.put({"type": "langsmith_sync_done", "dataset_id": dataset_id})
    except Exception as exc:
        events.put({"type": "langsmith_sync_error", "error": str(exc)})
        raise

    pipeline_results: dict[str, Any] = {}

    for pipeline_name in ("legacy", "langgraph"):
        events.put({
            "type": "langsmith_pipeline_start",
            "pipeline": pipeline_name,
            "message": f"Running {pipeline_name} pipeline via LangSmith evaluate()…",
        })
        t0 = time.perf_counter()
        try:
            exp = evaluate(
                _make_pipeline_target(pipeline_name, tools),
                data=DATASET_NAME,
                evaluators=[_make_criteria_evaluator(), _make_role_evaluator()],
                experiment_prefix=f"fronei-{pipeline_name}-{run_id[:8]}",
                max_concurrency=max_concurrency,
                metadata={"run_id": run_id, "pipeline": pipeline_name},
            )
            elapsed = round(time.perf_counter() - t0, 1)
            exp_url = getattr(exp, "url", None) or getattr(exp, "experiment_url", None)
            pipeline_results[pipeline_name] = {
                "ok": True,
                "experiment_url": exp_url,
                "elapsed_s": elapsed,
            }
            events.put({
                "type": "langsmith_pipeline_done",
                "pipeline": pipeline_name,
                "experiment_url": exp_url,
                "elapsed_s": elapsed,
            })
        except Exception as exc:
            elapsed = round(time.perf_counter() - t0, 1)
            pipeline_results[pipeline_name] = {"ok": False, "error": str(exc), "elapsed_s": elapsed}
            events.put({
                "type": "langsmith_pipeline_error",
                "pipeline": pipeline_name,
                "error": str(exc),
            })
            log.error("LangSmith eval failed for pipeline %s: %s", pipeline_name, exc)

    return {
        "mode": "langsmith",
        "dataset_id": dataset_id,
        "run_id": run_id,
        "pipelines": pipeline_results,
        "legacy_experiment_url": pipeline_results.get("legacy", {}).get("experiment_url"),
        "langgraph_experiment_url": pipeline_results.get("langgraph", {}).get("experiment_url"),
    }
