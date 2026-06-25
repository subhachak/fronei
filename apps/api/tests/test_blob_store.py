from __future__ import annotations

import base64
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.models import Artifact, Base, Turn
from app.services import artifact_migration
from app.services.blob_store import LocalBlobStore, S3BlobStore


def test_local_blob_store_round_trip(tmp_path):
    store = LocalBlobStore(tmp_path / "artifacts")
    stored = store.put("user/turn/report.docx", b"document", content_type="application/test")

    assert stored.location == "local:user/turn/report.docx"
    assert stored.size_bytes == 8
    assert store.read(stored.location) == b"document"
    assert store.presigned_download_url(stored.location, filename="report.docx", content_type="application/test") is None

    store.delete(stored.location)
    assert not (tmp_path / "artifacts/user/turn/report.docx").exists()


class _FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.presign = None

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs):
        payload = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": _Body(payload)}

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presign = (operation, Params, ExpiresIn)
        return "https://objects.example/signed"


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


def test_s3_blob_store_uses_private_object_keys_and_presigned_downloads():
    store = object.__new__(S3BlobStore)
    store.client = _FakeS3Client()
    store.bucket = "private"
    store.prefix = "fronei/artifacts"
    store.ttl_seconds = 300

    stored = store.put("user/turn/report.docx", b"document", content_type="application/test")

    assert stored.location == "s3:user/turn/report.docx"
    assert store.read(stored.location) == b"document"
    assert store.presigned_download_url(
        stored.location,
        filename="Quarterly report.docx",
        content_type="application/test",
    ) == "https://objects.example/signed"
    _, params, ttl = store.client.presign
    assert params["Bucket"] == "private"
    assert params["Key"] == "fronei/artifacts/user/turn/report.docx"
    assert "Quarterly%20report.docx" in params["ResponseContentDisposition"]
    assert ttl == 300


def test_artifact_migration_moves_base64_rows_to_configured_store(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    target = LocalBlobStore(tmp_path / "target")
    monkeypatch.setattr(artifact_migration, "SessionLocal", Session)
    monkeypatch.setattr(artifact_migration, "get_blob_store", lambda: target)
    with Session() as db:
        db.add(Turn(
            id="turn_1",
            user_id="u1",
            conversation_id=None,
            objective="Create report",
            route="research_document",
            quality_mode="standard",
            status="completed",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
        db.add(Artifact(
            id="artifact_1",
            turn_id="turn_1",
            kind="docx",
            filename="report.docx",
            mime_type="application/test",
            base64_data=base64.b64encode(b"document").decode("ascii"),
        ))
        db.commit()

    counts = artifact_migration.migrate_artifacts()

    assert counts["migrated"] == 1
    with Session() as db:
        row = db.get(Artifact, "artifact_1")
        assert row.base64_data == ""
        assert row.storage_path.startswith("local:")
        assert target.read(row.storage_path) == b"document"


def test_s3_settings_require_bucket_in_production(monkeypatch):
    settings = Settings(
        app_env="production",
        clerk_issuer="https://issuer.example",
        clerk_audience="fronei",
        admin_user_ids="admin",
        artifact_storage_backend="s3",
        artifact_s3_bucket="",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    from app.config import check_production_config

    try:
        check_production_config()
    except RuntimeError as exc:
        assert "ARTIFACT_S3_BUCKET" in str(exc)
    else:
        raise AssertionError("Missing S3 bucket should fail production configuration.")
