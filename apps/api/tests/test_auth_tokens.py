from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app import auth
from app.config import Settings, check_production_config, get_settings


@pytest.fixture
def token_keys(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    monkeypatch.setattr(
        auth,
        "_jwks_client",
        lambda: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
        ),
    )
    return private_key


@pytest.fixture
def production_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.example")
    monkeypatch.setenv("CLERK_AUDIENCE", "fronei-api")
    monkeypatch.setenv(
        "CLERK_AUTHORIZED_PARTIES",
        "https://fronei.com,https://www.fronei.com",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _token(
    private_key,
    *,
    issuer: str = "https://clerk.example",
    audience: str = "fronei-api",
    authorized_party: str = "https://fronei.com",
    expires_delta: timedelta = timedelta(minutes=5),
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "user_123",
            "sid": "sess_123",
            "iss": issuer,
            "aud": audience,
            "azp": authorized_party,
            "iat": now,
            "nbf": now,
            "exp": now + expires_delta,
        },
        private_key,
        algorithm="RS256",
    )


def test_valid_production_token_is_accepted(token_keys, production_auth):
    payload = auth._decode_token(_token(token_keys))

    assert payload["sub"] == "user_123"


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("issuer", "https://other-clerk.example"),
        ("audience", "another-api"),
        ("authorized_party", "https://attacker.example"),
    ],
)
def test_production_token_rejects_wrong_boundary_claim(
    token_keys,
    production_auth,
    claim,
    value,
):
    kwargs = {claim: value}

    with pytest.raises(HTTPException) as exc_info:
        auth._decode_token(_token(token_keys, **kwargs))

    assert exc_info.value.status_code == 401


def test_production_token_rejects_expired_token(token_keys, production_auth):
    with pytest.raises(HTTPException) as exc_info:
        auth._decode_token(
            _token(token_keys, expires_delta=timedelta(minutes=-1))
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Token expired"


def test_production_config_requires_authorized_parties(monkeypatch):
    settings = Settings(
        app_env="production",
        clerk_issuer="https://clerk.example",
        clerk_audience="fronei-api",
        clerk_authorized_parties="",
        admin_user_ids="admin",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="CLERK_AUTHORIZED_PARTIES"):
        check_production_config()


@pytest.mark.parametrize(
    "authorized_parties",
    ["*", "http://fronei.com", "https://localhost:3000", "https://fronei.com/app"],
)
def test_production_config_rejects_invalid_authorized_parties(
    monkeypatch,
    authorized_parties,
):
    settings = Settings(
        app_env="production",
        clerk_issuer="https://clerk.example",
        clerk_audience="fronei-api",
        clerk_authorized_parties=authorized_parties,
        admin_user_ids="admin",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="HTTPS origin"):
        check_production_config()
