"""Bootstrap eval cases from the golden set into the eval_cases table.

Run from apps/api:
    python -m scripts.seed_eval_cases [--force]

Options:
    --force   Re-seed all cases even if they already exist (updates in-place).

The script is idempotent by default: existing cases (matched by title prefix
"[golden] <id>") are skipped unless --force is passed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

GOLDEN_SET_PATH = Path(__file__).resolve().parents[1] / "evals" / "research_golden_set.json"
SEEDED_BY = "seed_eval_cases"
TITLE_PREFIX = "[golden] "


def _criteria_from_entry(entry: dict) -> list[str]:
    """Convert unacceptable_failure_modes into positive criteria strings."""
    expected = entry.get("expected", {})
    failure_modes = expected.get("unacceptable_failure_modes", [])
    criteria = []

    # Invert each failure mode into a positive "must" statement
    for fm in failure_modes:
        fm = fm.strip().rstrip(".")
        criteria.append(f"Must NOT: {fm}")

    # Add a positive criterion for primary evidence role if present
    role = expected.get("primary_evidence_role")
    if role:
        criteria.append(f"Primary evidence role should be: {role}")

    return criteria


def _notes_from_entry(entry: dict) -> str | None:
    parts = []
    if entry.get("description"):
        parts.append(entry["description"])
    notes = entry.get("expected", {}).get("notes")
    if notes:
        parts.append(notes)
    return "\n\n".join(parts) if parts else None


def seed(force: bool = False) -> None:
    # Lazy import so the script can be called without the full app initialized
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app.db.models import EvalCase, SessionLocal  # noqa: PLC0415

    golden_set = json.loads(GOLDEN_SET_PATH.read_text())
    db = SessionLocal()

    try:
        created = 0
        updated = 0
        skipped = 0

        for entry in golden_set:
            title = f"{TITLE_PREFIX}{entry['id']}"
            expected = entry.get("expected", {})
            query = entry["request"]["message"]
            category = entry.get("category")
            role = expected.get("primary_evidence_role")
            min_src = expected.get("min_independent_sources")
            criteria = _criteria_from_entry(entry)
            notes = _notes_from_entry(entry)
            now = datetime.now(timezone.utc)

            existing = db.query(EvalCase).filter(EvalCase.title == title).first()

            if existing:
                if not force:
                    print(f"  skip   {title}")
                    skipped += 1
                    continue
                # Update in place
                existing.query = query
                existing.category = category
                existing.expected_criteria_json = json.dumps(criteria)
                existing.expected_primary_role = role
                existing.min_independent_sources = min_src
                existing.notes = notes
                existing.updated_at = now
                print(f"  update {title}")
                updated += 1
            else:
                case = EvalCase(
                    title=title,
                    query=query,
                    category=category,
                    expected_criteria_json=json.dumps(criteria),
                    expected_primary_role=role,
                    min_independent_sources=min_src,
                    notes=notes,
                    created_by=SEEDED_BY,
                    created_at=now,
                    updated_at=now,
                )
                db.add(case)
                print(f"  create {title}")
                created += 1

        db.commit()
        print(f"\nDone — {created} created, {updated} updated, {skipped} skipped.")

    finally:
        db.close()


if __name__ == "__main__":
    force = "--force" in sys.argv
    seed(force=force)
