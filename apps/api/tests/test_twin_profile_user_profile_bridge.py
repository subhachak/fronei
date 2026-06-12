import json
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, TwinProfile, UserProfile, WritingSample
from app.routers.twin_profile import _profile_out
from app.services.memory_consolidator import _merge_style_fallback, _style_context


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _fingerprint(**overrides) -> dict:
    data = {
        "sentence_length": "short",
        "formality": "professional",
        "directness": "high",
        "hedging": "low",
        "structure": "bullet_heavy",
        "technical_depth": "expert",
        "preferred_phrases": ["net-net"],
        "forbidden_phrases": ["leverage"],
        "avoid_patterns": ["long preambles"],
        "signature_patterns": ["crisp trade-offs"],
        "tone_by_audience": {"executive": "direct and outcome-focused"},
    }
    data.update(overrides)
    return data


def test_profile_out_prefers_user_profile_communication_style():
    legacy = TwinProfile(
        user_id="u1",
        fingerprint_json=json.dumps(_fingerprint(directness="medium")),
        rewrite_prompt="Rewrite this.",
        extracted_at=datetime.now(timezone.utc),
    )
    user_profile = UserProfile(
        user_id="u1",
        profile_json=json.dumps({"communication_style": _fingerprint(directness="high")}),
    )

    output = _profile_out(legacy, sample_count=2, user_profile=user_profile, user_id="u1")

    assert output.user_id == "u1"
    assert output.fingerprint is not None
    assert output.fingerprint.directness == "high"
    assert output.rewrite_prompt == "Rewrite this."


def test_style_context_loads_legacy_profile_and_recent_samples():
    db = _session()
    try:
        db.add(TwinProfile(
            user_id="u1",
            fingerprint_json=json.dumps(_fingerprint()),
            prefs_json=json.dumps({"forbidden_phrases": ["robust"]}),
        ))
        db.add(WritingSample(
            user_id="u1",
            content="This is a sufficiently long writing sample used for style analysis.",
            label="sample",
            char_count=66,
        ))
        db.commit()

        context = _style_context(db, "u1")

        assert context["fingerprint"]["directness"] == "high"
        assert context["prefs"]["forbidden_phrases"] == ["robust"]
        assert len(context["recent_samples"]) == 1
    finally:
        db.close()


def test_merge_style_fallback_adds_fingerprint_and_prefs():
    profile = _merge_style_fallback({}, {
        "fingerprint": _fingerprint(),
        "prefs": {"forbidden_phrases": ["synergy"]},
    })

    assert profile["communication_style"]["directness"] == "high"
    assert profile["communication_style"]["forbidden_phrases"] == ["leverage"]
