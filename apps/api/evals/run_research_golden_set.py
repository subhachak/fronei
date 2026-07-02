#!/usr/bin/env python3
"""Run the research golden eval set and dump per-case results for manual review.

Usage (from apps/api):
    python -m evals.run_research_golden_set

Requires configured search API keys (TAVILY_API_KEY / YOU_API_KEY / NIMBLE_API_KEY)
and a working database (for prompt resolution). Each run saves results to:
    apps/api/evals/research_golden_results/<id>.json

Grading is manual at this stage — compare before/after baselines by diffing
the output directory.

To establish the Phase 0 baseline before any pipeline changes:
    python -m evals.run_research_golden_set --tag baseline
Results are saved to: research_golden_results/<id>__baseline.json

Example diff:
    diff apps/api/evals/research_golden_results/h4_ead_operational_anchor__baseline.json \
         apps/api/evals/research_golden_results/h4_ead_operational_anchor.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the apps/api package root is on sys.path when run directly.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_API_ROOT = _SCRIPT_DIR.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


def _load_golden_set() -> list[dict]:
    path = _SCRIPT_DIR / "research_golden_set.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _make_tools():
    from app.services.agent.tools import Tools
    return Tools.from_settings()


def _build_request(entry: dict) -> "TurnRequest":  # noqa: F821
    """Build the request the exact way a real user turn would.

    The LangGraph research runner is invoked directly here, bypassing
    Runtime.run_stream(), but routing/tier resolution must still go through
    the real orchestrator.decide() so this
    eval exercises the same classification logic production uses — not a
    hand-rolled reimplementation of it. force_route="research" mirrors what
    happens when a user (or the UI) forces the research route; decide()
    still resolves research_level via choose_research_level() in that case
    (see orchestrator._normalize_research_decision).
    """
    from app.services.agent.models import TurnRequest
    from app.services.agent.orchestrator import decide

    forced_level = entry["request"].get("research_level", "auto")
    draft = TurnRequest(
        message=entry["request"]["message"],
        research_level=forced_level,
        quality_mode="standard",
        output_format="chat",
        force_route="research",
    )
    decision = decide(draft)
    return TurnRequest(
        message=entry["request"]["message"],
        research_level=decision.research_level,
        quality_mode="standard",
        output_format="chat",
    )


def _run_one_case(entry: dict, tools, run_id: str) -> dict:
    from app.services.agent.langgraph_runtime import run_langgraph_research

    request = _build_request(entry)

    stages: list[dict] = []

    def progress(stage: str, message: str, **data: dict) -> None:
        stages.append({"stage": stage, "message": message})

    started = time.perf_counter()
    error_info: str | None = None
    result: dict | None = None

    try:
        result = run_langgraph_research(request, tools, progress=progress)
    except Exception:
        error_info = traceback.format_exc()

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if result is None:
        return {
            "id": entry["id"],
            "run_id": run_id,
            "elapsed_ms": elapsed_ms,
            "error": error_info,
            "stages": stages,
        }

    evidence = result.get("evidence")
    answer: str = result.get("response", {}).text if hasattr(result.get("response"), "text") else str(result.get("response", ""))

    # Dump claim roles — this is what Phase 1 stop condition checks.
    claim_dump = [
        {
            "claim_id": claim.claim_id,
            "source_id": claim.source_id,
            "claim_type": claim.claim_type,
            "claim_role": claim.claim_role,
            "freshness_risk": claim.freshness_risk,
            "confidence": claim.confidence,
            "text": claim.text[:200],
            "source_url": claim.source_url,
        }
        for claim in (evidence.claims if evidence else [])
    ]

    # Dump evidence items with new metadata fields (Phase 3).
    item_dump = [
        {
            "source_id": item.source_id,
            "source_type": item.source_type,
            "title": item.title[:120],
            "url": item.url,
            "authority": item.authority,
            "relevance": item.relevance,
            # Phase 3 fields — present post-Phase 3, absent before:
            "date_confidence": getattr(item, "date_confidence", "unknown"),
            "published_date": getattr(item, "published_date", None),
            "source_family": getattr(item, "source_family", ""),
            "content_fingerprint": getattr(item, "content_fingerprint", ""),
        }
        for item in (evidence.items if evidence else [])
    ]

    plan = result.get("plan")
    feedback = result.get("feedback")

    output = {
        "id": entry["id"],
        "run_id": run_id,
        "elapsed_ms": elapsed_ms,
        "answer_preview": answer[:1200],
        "answer_length": len(answer),
        "evidence_items": item_dump,
        "claims": claim_dump,
        "claim_roles_present": sorted(set(c["claim_role"] for c in claim_dump)),
        "claim_types_present": sorted(set(c["claim_type"] for c in claim_dump)),
        # Phase 3: check independence proxy
        "source_families_present": sorted(set(i["source_family"] for i in item_dump if i["source_family"])),
        "independent_source_count": getattr(evidence, "independent_source_count", None) if evidence else None,
        "research_profile": plan.research_profile if plan else None,
        "expected_primary_role": getattr(plan, "expected_primary_role", None) if plan else None,
        "judge_score": feedback.final_score if feedback else None,
        "repaired": feedback.repaired if feedback else None,
        "repair_attempts": feedback.repair_attempts if feedback else None,
        "stages": [s["stage"] for s in stages],
        "expected": entry.get("expected", {}),
    }
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Run research golden eval set.")
    parser.add_argument("--tag", default="", help="Optional tag suffix for result filenames, e.g. 'baseline' or 'phase1'.")
    parser.add_argument("--ids", nargs="*", help="Run only specific case IDs. Default: all.")
    args = parser.parse_args()

    golden_set = _load_golden_set()
    if args.ids:
        golden_set = [e for e in golden_set if e["id"] in args.ids]
        if not golden_set:
            print(f"No cases matched IDs: {args.ids}")
            sys.exit(1)

    try:
        tools = _make_tools()
    except Exception as exc:
        print(f"ERROR: Could not initialise Tools: {exc}")
        print("Make sure TAVILY_API_KEY / YOU_API_KEY / NIMBLE_API_KEY is set.")
        sys.exit(1)

    results_dir = _SCRIPT_DIR / "research_golden_results"
    results_dir.mkdir(exist_ok=True)

    run_id = time.strftime("%Y%m%dT%H%M%S")
    suffix = f"__{args.tag}" if args.tag else ""
    passed = 0
    failed = 0

    for entry in golden_set:
        case_id = entry["id"]
        print(f"\n--- Running case: {case_id} ---")
        print(f"  Query: {entry['request']['message'][:100]}")
        print(f"  Expected primary role: {entry['expected'].get('primary_evidence_role', 'n/a')}")

        output = _run_one_case(entry, tools, run_id)

        filename = f"{case_id}{suffix}.json"
        result_path = results_dir / filename
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, indent=2, default=str)

        if output.get("error"):
            failed += 1
            print(f"  FAILED: {output['error'][:200]}")
        else:
            passed += 1
            roles = output.get("claim_roles_present", [])
            print(f"  OK — {len(output.get('evidence_items', []))} items, {len(output.get('claims', []))} claims")
            print(f"  Claim roles present: {roles}")
            print(f"  Answer length: {output.get('answer_length', 0)} chars")
            print(f"  Saved to: {result_path}")

    print(f"\n=== Golden eval complete: {passed} passed, {failed} failed (run_id={run_id}) ===")
    print(f"\nManual grading checklist per case:")
    print("  1. For anchor (h4_ead_operational_anchor): is 'operational_reality' in claim_roles_present?")
    print("  2. For policy cases: is 'official_policy' the dominant role?")
    print("  3. For conflict cases: does answer_preview name BOTH positions?")
    print("  4. For stale cases: does answer_preview flag the date?")
    print("  5. For duplicate case: is independent_source_count < len(evidence_items)?")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
