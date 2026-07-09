"""Phase 2.1 — production must not silently fall back to an ephemeral sqlite
DATABASE_URL. Render/most PaaS filesystems wipe local files on every
redeploy/restart, so an unset or sqlite:// DATABASE_URL in production is a
data-loss bug, not a valid configuration.
"""
from __future__ import annotations

import pytest

from app.config import Settings, check_production_config


def _valid_production_settings(**overrides) -> Settings:
    base = dict(
        app_env="production",
        clerk_issuer="https://issuer.example",
        clerk_audience="fronei",
        clerk_authorized_parties="https://fronei.example",
        admin_user_ids="admin",
    )
    base.update(overrides)
    return Settings(**base)


def test_production_rejects_default_sqlite_database_url(monkeypatch):
    # The Settings default (database_url unset) is sqlite:///./fronei.db.
    settings = _valid_production_settings()
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        check_production_config()


def test_production_rejects_explicit_sqlite_database_url(monkeypatch):
    settings = _valid_production_settings(database_url="sqlite:////tmp/prod.db")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        check_production_config()


def test_production_accepts_postgres_database_url(monkeypatch):
    settings = _valid_production_settings(
        database_url="postgresql://user:pass@ep-xxx.us-east-1.aws.neon.tech/neondb?sslmode=require"
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    check_production_config()  # should not raise


def test_non_production_allows_sqlite_database_url(monkeypatch):
    settings = Settings(app_env="local")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    check_production_config()  # should not raise -- local/dev is exempt
