"""
Usage: uv run --extra dev python scripts/measure_latency.py [--min-turns 20] [--since YYYY-MM-DD]

Prints p50/p95/p99 latency_ms per route from the turns table.

Gate routes: direct, clarify (fast-path routes expected under 20s).
Research/document routes are excluded from the gate — they are expected to be slow.
Routes with fewer than --min-turns samples are flagged as insufficient.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timezone

from app.db.models import SessionLocal, Turn

TARGET_MS = 20_000
# Only these routes are subject to the p95 < 20s gate.
GATE_ROUTES = {"direct", "clarify"}


def _percentile(sorted_samples: list[int], percentile: float) -> int:
    if not sorted_samples:
        return 0
    index = min(len(sorted_samples) - 1, int(len(sorted_samples) * percentile))
    return sorted_samples[index]


def main(min_turns: int = 20, since: datetime | None = None) -> None:
    db = SessionLocal()
    try:
        q = db.query(Turn.route, Turn.latency_ms).filter(
            Turn.latency_ms.isnot(None), Turn.latency_ms > 0
        )
        if since is not None:
            q = q.filter(Turn.created_at >= since)
        rows = q.all()
    finally:
        db.close()

    by_route: dict[str, list[int]] = {}
    for route, ms in rows:
        by_route.setdefault(route or "unknown", []).append(int(ms))

    gate_passed = True
    evaluated_routes = 0
    since_label = since.date().isoformat() if since else "all time"
    print(f"\nLatency report — {since_label} — gate routes: {', '.join(sorted(GATE_ROUTES))}")
    print(f"\n{'Route':<28} {'N':>5}  {'p50':>7}  {'p95':>7}  {'p99':>7}  gate")
    print("-" * 70)
    for route, samples in sorted(by_route.items()):
        sample_count = len(samples)
        sorted_samples = sorted(samples)
        p50 = statistics.median(sorted_samples)
        p95 = _percentile(sorted_samples, 0.95)
        p99 = _percentile(sorted_samples, 0.99)
        in_gate = route in GATE_ROUTES
        insufficient = sample_count < min_turns
        if not in_gate:
            gate = "n/a"
        elif insufficient:
            gate = "SKIP(n<min)"
        else:
            gate = "PASS" if p95 < TARGET_MS else "FAIL"
            evaluated_routes += 1
            if gate == "FAIL":
                gate_passed = False
        print(f"{route:<28} {sample_count:>5}  {p50:>7.0f}  {p95:>7.0f}  {p99:>7.0f}  {gate}")

    print()
    if evaluated_routes == 0:
        print("M1 latency gate: PENDING — no gate route has enough post-deployment samples yet")
    elif gate_passed:
        print("M1 latency gate: PASSED")
    else:
        print("M1 latency gate: FAILED — p95 > 20 000 ms on one or more gate routes")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-turns", type=int, default=20)
    parser.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc),
        default=None,
        help="Only include turns created on or after this date (YYYY-MM-DD).",
    )
    args = parser.parse_args()
    main(args.min_turns, args.since)
