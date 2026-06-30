#!/usr/bin/env python3
"""Run the LangGraph vs legacy parity comparator over the research golden set.

Usage (from apps/api):
    python -m evals.run_parity_comparator [--ids id1 id2 ...] [--tag tag] [--markdown]

Both pipelines run with the SAME live Tools instance (real search API keys
required).  Results are written to:
    apps/api/evals/parity_results/parity_<run_id>[__<tag>].json
    apps/api/evals/parity_results/parity_<run_id>[__<tag>].md   (if --markdown)

The markdown report is also printed to stdout for use as a GitHub Actions
job summary ($GITHUB_STEP_SUMMARY).

Exit codes:
  0 — all parity gates pass (cutover recommended)
  1 — one or more gates fail (review blockers in the report)
  2 — tool initialisation failed (missing API keys)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_API_ROOT = _SCRIPT_DIR.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


def _load_golden_set(ids: list[str] | None = None) -> list[dict]:
    path = _SCRIPT_DIR / "research_golden_set.json"
    with open(path, encoding="utf-8") as fh:
        cases = json.load(fh)
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases


def _make_tools():
    from app.services.agent.tools import Tools
    return Tools.from_settings()


def _build_request(entry: dict) -> "TurnRequest":  # noqa: F821
    """Build the request the exact way a real user turn would.

    Both pipelines below are invoked directly, bypassing Runtime.run_stream(),
    but routing/tier resolution must still go through the real
    orchestrator.decide() so this eval exercises the same classification
    logic production uses — not a hand-rolled reimplementation of it.
    force_route="research" mirrors what happens when a user (or the UI)
    forces the research route; decide() still resolves research_level via
    choose_research_level() in that case (see
    orchestrator._normalize_research_decision).
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


def _run_legacy(entry: dict, tools) -> tuple[dict | None, str | None]:
    """Run one case through the legacy pipeline. Returns (result, error)."""
    from app.services.agent.research_lead import lead_research_loop

    request = _build_request(entry)
    try:
        result = lead_research_loop(request, tools, progress=None)
        return result, None
    except Exception:
        return None, traceback.format_exc()


def _run_langgraph(entry: dict, tools) -> tuple[dict | None, str | None]:
    """Run one case through the LangGraph pipeline. Returns (result, error)."""
    from app.services.agent.langgraph_runtime.runtime import run_langgraph_research

    request = _build_request(entry)
    try:
        result = run_langgraph_research(request, tools, progress=None)
        return result, None
    except Exception:
        return None, traceback.format_exc()


def _make_report_row(case_id: str, legacy_result, langgraph_result, legacy_err, langgraph_err) -> dict:
    """Build per-case report dict from live pipeline results."""
    from app.services.agent.langgraph_runtime.comparators import compare_pipeline_results

    pr = compare_pipeline_results(
        case_id=case_id,
        legacy_result=legacy_result,
        langgraph_result=langgraph_result,
        legacy_error=legacy_err,
        langgraph_error=langgraph_err,
    )
    return pr.to_dict()


def _render_markdown(report_dict: dict) -> str:
    """Render the aggregate parity report as GitHub-flavoured markdown."""
    lines = []
    lines.append("# LangGraph Parity Report")
    lines.append("")

    cutover = report_dict.get("cutover_recommended", False)
    verdict_emoji = "✅" if cutover else "❌"
    lines.append(f"## Overall Verdict: {verdict_emoji} {'CUTOVER RECOMMENDED' if cutover else 'NOT READY'}")
    lines.append("")

    blockers = report_dict.get("cutover_blockers", [])
    if blockers:
        lines.append("### Blockers")
        for b in blockers:
            lines.append(f"- {b}")
        lines.append("")

    n = report_dict.get("total_cases", 0)
    lines.append("### Gate Summary")
    lines.append("")
    lines.append("| Gate | Pass | Total | Rate |")
    lines.append("|------|------|-------|------|")

    def _gate_row(label: str, passed: int) -> str:
        rate = f"{passed / n * 100:.0f}%" if n else "–"
        emoji = "✅" if passed == n else ("⚠️" if passed >= n * 0.8 else "❌")
        return f"| {label} | {passed} | {n} | {rate} {emoji} |"

    lines.append(_gate_row("Structural (no crash)", report_dict.get("structural_pass", 0)))
    lines.append(_gate_row("Answer length ≥70% of legacy", report_dict.get("answer_length_gate_pass", 0)))
    lines.append(_gate_row("Evidence count ≥80% of legacy", report_dict.get("evidence_gate_pass", 0)))
    lines.append(_gate_row("Claim count ≥70% of legacy", report_dict.get("claim_gate_pass", 0)))
    lines.append(_gate_row("Budget ≤1.5× legacy", report_dict.get("budget_gate_pass", 0)))
    lines.append(_gate_row("Judge verdict agreement", report_dict.get("verdict_agree", 0)))
    lines.append("")

    lines.append("### Median Ratios (LangGraph / Legacy)")
    lines.append("")
    lines.append("| Metric | Median Ratio |")
    lines.append("|--------|-------------|")

    def _ratio_row(label: str, key: str) -> str:
        v = report_dict.get(key)
        s = f"{v:.2f}" if v is not None else "–"
        return f"| {label} | {s} |"

    lines.append(_ratio_row("Answer length", "median_answer_length_ratio"))
    lines.append(_ratio_row("Evidence items", "median_evidence_count_ratio"))
    lines.append(_ratio_row("Claims", "median_claim_count_ratio"))
    lines.append(_ratio_row("Cost USD", "median_cost_ratio"))
    lines.append("")

    per_case = report_dict.get("per_case", [])
    if per_case:
        lines.append("### Per-Case Results")
        lines.append("")
        lines.append("| Case ID | Structural | Ans ratio | Evid ratio | Claim ratio | Judge agree | Pass |")
        lines.append("|---------|-----------|-----------|------------|-------------|-------------|------|")
        for r in per_case:
            def _yn(v): return "✅" if v else "❌"
            def _ratio(v): return f"{v:.2f}" if v is not None else "–"
            lines.append(
                f"| {r['case_id']} "
                f"| {_yn(r.get('passes_structural_gate'))} "
                f"| {_ratio(r.get('answer_length_ratio'))} "
                f"| {_ratio(r.get('evidence_count_ratio'))} "
                f"| {_ratio(r.get('claim_count_ratio'))} "
                f"| {_yn(r.get('judge_verdict_agrees'))} "
                f"| {_yn(r.get('overall_pass'))} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph vs legacy parity comparator.")
    parser.add_argument("--ids", nargs="*", help="Run only specific case IDs.")
    parser.add_argument("--tag", default="", help="Optional tag suffix for result filenames.")
    parser.add_argument("--markdown", action="store_true", help="Write a .md report alongside the JSON.")
    args = parser.parse_args()

    golden_set = _load_golden_set(args.ids)
    if not golden_set:
        print("No cases to run.")
        sys.exit(1)

    try:
        tools = _make_tools()
    except Exception as exc:
        print(f"ERROR: Could not initialise Tools: {exc}")
        print("Make sure TAVILY_API_KEY / YOU_API_KEY / NIMBLE_API_KEY is configured.")
        sys.exit(2)

    results_dir = _SCRIPT_DIR / "parity_results"
    results_dir.mkdir(exist_ok=True)

    run_id = time.strftime("%Y%m%dT%H%M%S")
    suffix = f"__{args.tag}" if args.tag else ""

    from app.services.agent.langgraph_runtime.comparators import (
        ParityResult,
        aggregate_parity_results,
    )

    per_case_results: list[ParityResult] = []
    per_case_rows: list[dict] = []

    for entry in golden_set:
        case_id = entry["id"]
        print(f"\n--- {case_id} ---")
        print(f"  Query: {entry['request']['message'][:90]}")

        t0 = time.perf_counter()
        legacy_result, legacy_err = _run_legacy(entry, tools)
        legacy_ms = int((time.perf_counter() - t0) * 1000)
        print(f"  Legacy:    {'ERROR' if legacy_err else 'OK'} ({legacy_ms}ms)")

        t0 = time.perf_counter()
        langgraph_result, langgraph_err = _run_langgraph(entry, tools)
        lg_ms = int((time.perf_counter() - t0) * 1000)
        print(f"  LangGraph: {'ERROR' if langgraph_err else 'OK'} ({lg_ms}ms)")

        from app.services.agent.langgraph_runtime.comparators import compare_pipeline_results
        pr = compare_pipeline_results(
            case_id=case_id,
            legacy_result=legacy_result,
            langgraph_result=langgraph_result,
            legacy_error=legacy_err,
            langgraph_error=langgraph_err,
        )
        per_case_results.append(pr)
        row = pr.to_dict()
        row["legacy_ms"] = legacy_ms
        row["langgraph_ms"] = lg_ms
        per_case_rows.append(row)

        gate_icon = "✅" if pr.overall_pass else "❌"
        print(
            f"  {gate_icon} ans={pr.answer_length_ratio:.2f if pr.answer_length_ratio is not None else '–'} "
            f"evid={pr.evidence_count_ratio:.2f if pr.evidence_count_ratio is not None else '–'} "
            f"claims={pr.claim_count_ratio:.2f if pr.claim_count_ratio is not None else '–'} "
            f"verdict_agree={pr.judge_verdict_agrees}"
        )

    # Aggregate
    report = aggregate_parity_results(per_case_results)
    report_dict = report.to_dict()
    # Replace per_case with the enriched rows (include timing)
    report_dict["per_case"] = per_case_rows

    # Save JSON
    json_path = results_dir / f"parity_{run_id}{suffix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report_dict, fh, indent=2, default=str)
    print(f"\nJSON report saved: {json_path}")

    # Save + print markdown
    md = _render_markdown(report_dict)
    if args.markdown:
        md_path = results_dir / f"parity_{run_id}{suffix}.md"
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"Markdown report saved: {md_path}")

    # Write to GitHub step summary if running in Actions
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        with open(github_summary, "a", encoding="utf-8") as fh:
            fh.write(md)

    print("\n" + "=" * 70)
    print(md.split("### Gate Summary")[0])  # Print header only to stdout
    for b in report.cutover_blockers:
        print(f"  BLOCKER: {b}")
    print(
        f"\n{'CUTOVER RECOMMENDED ✅' if report.cutover_recommended else 'NOT READY ❌'} "
        f"({report.overall_pass}/{report.total_cases} cases pass all gates)"
    )

    sys.exit(0 if report.cutover_recommended else 1)


if __name__ == "__main__":
    main()
