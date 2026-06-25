from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.config import Settings, get_settings


@dataclass(frozen=True)
class StoredBlob:
    location: str
    size_bytes: int
    sha256: str


class BlobStore(Protocol):
    scheme: str

    def put(self, key: str, payload: bytes, *, content_type: str) -> StoredBlob: ...
    def read(self, location: str) -> bytes: ...
    def delete(self, location: str) -> None: ...
    def presigned_download_url(self, location: str, *, filename: str, content_type: str) -> str | None: ...


def _safe_key(key: str) -> str:
    parts = []
    for raw in key.replace("\\", "/").split("/"):
        cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)
        if cleaned and cleaned not in {".", ".."}:
            parts.append(cleaned[:180])
    if not parts:
        raise ValueError("Blob key must contain at least one safe path segment.")
    return "/".join(parts)


def _location_key(location: str, scheme: str) -> str:
    prefix = f"{scheme}:"
    if not location.startswith(prefix):
        raise ValueError(f"Expected {scheme} blob location.")
    return _safe_key(location[len(prefix):])


class LocalBlobStore:
    scheme = "local"

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        path.relative_to(self.root)
        return path

    def put(self, key: str, payload: bytes, *, content_type: str) -> StoredBlob:
        safe_key = _safe_key(key)
        path = self._path(safe_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return StoredBlob(
            location=f"{self.scheme}:{safe_key}",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def read(self, location: str) -> bytes:
        return self._path(_location_key(location, self.scheme)).read_bytes()

    def delete(self, location: str) -> None:
        self._path(_location_key(location, self.scheme)).unlink(missing_ok=True)

    def presigned_download_url(self, location: str, *, filename: str, content_type: str) -> None:
        return None


class S3BlobStore:
    scheme = "s3"

    def __init__(self, settings: Settings):
        if not settings.artifact_s3_bucket:
            raise ValueError("ARTIFACT_S3_BUCKET is required for the S3 artifact backend.")
        import boto3

        kwargs = {
            "service_name": "s3",
            "region_name": settings.artifact_s3_region or None,
        }
        if settings.artifact_s3_endpoint_url:
            kwargs["endpoint_url"] = settings.artifact_s3_endpoint_url
        if settings.artifact_s3_access_key_id:
            kwargs["aws_access_key_id"] = settings.artifact_s3_access_key_id
        if settings.artifact_s3_secret_access_key:
            kwargs["aws_secret_access_key"] = settings.artifact_s3_secret_access_key
        self.client = boto3.client(**kwargs)
        self.bucket = settings.artifact_s3_bucket
        self.prefix = settings.artifact_s3_key_prefix.strip("/")
        self.ttl_seconds = max(60, min(3600, settings.artifact_download_url_ttl_seconds))

    def _object_key(self, location_or_key: str) -> str:
        key = (
            _location_key(location_or_key, self.scheme)
            if location_or_key.startswith(f"{self.scheme}:")
            else _safe_key(location_or_key)
        )
        return f"{self.prefix}/{key}" if self.prefix else key

    def _location_from_object_key(self, object_key: str) -> str:
        if self.prefix and object_key.startswith(f"{self.prefix}/"):
            object_key = object_key[len(self.prefix) + 1:]
        return f"{self.scheme}:{_safe_key(object_key)}"

    def put(self, key: str, payload: bytes, *, content_type: str) -> StoredBlob:
        object_key = self._object_key(key)
        digest = hashlib.sha256(payload).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=payload,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        return StoredBlob(
            location=self._location_from_object_key(object_key),
            size_bytes=len(payload),
            sha256=digest,
        )

    def read(self, location: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(location))
        return response["Body"].read()

    def delete(self, location: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._object_key(location))

    def presigned_download_url(self, location: str, *, filename: str, content_type: str) -> str:
        disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": self._object_key(location),
                "ResponseContentDisposition": disposition,
                "ResponseContentType": content_type,
            },
            ExpiresIn=self.ttl_seconds,
        )


def get_blob_store(settings: Settings | None = None) -> BlobStore:
    settings = settings or get_settings()
    backend = settings.artifact_storage_backend.strip().lower()
    if backend == "s3":
        return S3BlobStore(settings)
    if backend != "local":
        raise ValueError(f"Unsupported artifact storage backend: {backend}")
    return LocalBlobStore(Path(settings.artifact_storage_dir))


def store_for_location(location: str, settings: Settings | None = None) -> BlobStore:
    settings = settings or get_settings()
    if location.startswith("s3:"):
        return S3BlobStore(settings)
    return LocalBlobStore(Path(settings.artifact_storage_dir))


def read_legacy_local_path(location: str, settings: Settings | None = None) -> bytes:
    settings = settings or get_settings()
    root = Path(settings.artifact_storage_dir).expanduser().resolve()
    path = Path(location).expanduser().resolve()
    path.relative_to(root)
    return path.read_bytes()


def delete_blob_location(location: str, settings: Settings | None = None) -> None:
    if not location:
        return
    if location.startswith(("local:", "s3:")):
        store_for_location(location, settings).delete(location)
        return
    try:
        path = Path(location).expanduser().resolve()
        root = Path((settings or get_settings()).artifact_storage_dir).expanduser().resolve()
        path.relative_to(root)
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        return
