from __future__ import annotations

import argparse
import base64
import logging

from app.db.models import Artifact, SessionLocal, Turn
from app.services.agent.persistence import artifact_blob_key
from app.services.blob_store import (
    get_blob_store,
    read_legacy_local_path,
    store_for_location,
)


logger = logging.getLogger(__name__)


def _read_existing(row: Artifact) -> bytes | None:
    if row.storage_path:
        if row.storage_path.startswith(("local:", "s3:")):
            return store_for_location(row.storage_path).read(row.storage_path)
        return read_legacy_local_path(row.storage_path)
    if row.base64_data:
        return base64.b64decode(row.base64_data)
    return None


def migrate_artifacts(*, dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    target = get_blob_store()
    counts = {"scanned": 0, "migrated": 0, "skipped": 0, "missing": 0, "failed": 0}
    db = SessionLocal()
    try:
        query = db.query(Artifact).order_by(Artifact.created_at.asc())
        if limit:
            query = query.limit(max(1, limit))
        for row in query.all():
            counts["scanned"] += 1
            if row.storage_path and row.storage_path.startswith(f"{target.scheme}:"):
                counts["skipped"] += 1
                continue
            turn = db.get(Turn, row.turn_id)
            if turn is None:
                counts["missing"] += 1
                continue
            try:
                payload = _read_existing(row)
                if payload is None:
                    counts["missing"] += 1
                    continue
                if dry_run:
                    counts["migrated"] += 1
                    continue
                stored = target.put(
                    artifact_blob_key(turn.user_id, row.turn_id, row.id, row.filename),
                    payload,
                    content_type=row.mime_type,
                )
                row.storage_path = stored.location
                row.base64_data = ""
                row.size_bytes = stored.size_bytes
                row.sha256 = stored.sha256
                db.commit()
                counts["migrated"] += 1
            except Exception:
                db.rollback()
                counts["failed"] += 1
                logger.exception("Artifact migration failed for %s", row.id)
        return counts
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy artifacts to the configured blob store.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(migrate_artifacts(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
