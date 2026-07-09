from __future__ import annotations

from types import SimpleNamespace

from app.services.agent import fact_extractor
from app.services.agent import model_client
from app.services.agent.research_utils import temporal_context


def test_extract_and_store_facts_stores_as_of_date(monkeypatch):
    calls = []
    captured_prompt = {}

    def fake_simple_completion(_system, user, **_kwargs):
        captured_prompt["user"] = user
        return SimpleNamespace(
            text='[{"entity_id":"workspace_1","entity_type":"workspace","fact_key":"stack","fact_value":"Postgres"}]'
        )

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    monkeypatch.setattr(fact_extractor, "upsert_fact", lambda *args, **kwargs: calls.append((args, kwargs)))

    stored = fact_extractor.extract_and_store_facts("user_1", "conv_1", "Synthesis", db="db")

    current_date = temporal_context()["current_date"]
    assert stored == 1
    assert calls[0][1]["as_of_date"] == current_date
    assert captured_prompt["user"].startswith(f"Current date: {current_date}")


def test_extract_and_store_facts_valid_response_upserts_two(monkeypatch):
    calls = []

    monkeypatch.setattr(
        model_client,
        "simple_completion",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=(
                "["
                '{"entity_id":"workspace_1","entity_type":"workspace","fact_key":"stack","fact_value":"Postgres"},'
                '{"entity_id":"workspace_1","entity_type":"workspace","fact_key":"memory_layer","fact_value":"L3"}'
                "]"
            )
        ),
    )

    def fake_upsert(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(fact_extractor, "upsert_fact", fake_upsert)

    stored = fact_extractor.extract_and_store_facts("user_1", "conv_1", "Synthesis", db="db")

    assert stored == 2
    assert calls[0][0][:5] == ("user_1", "workspace_1", "workspace", "stack", "Postgres")
    assert calls[0][1]["source_conversation_id"] == "conv_1"
    assert calls[0][1]["db"] == "db"
    assert calls[1][0][:5] == ("user_1", "workspace_1", "workspace", "memory_layer", "L3")


def test_extract_and_store_facts_malformed_json_returns_zero(monkeypatch):
    monkeypatch.setattr(
        model_client,
        "simple_completion",
        lambda *_args, **_kwargs: SimpleNamespace(text="{not json"),
    )

    assert fact_extractor.extract_and_store_facts("user_1", "conv_1", "Synthesis", db="db") == 0


def test_extract_and_store_facts_skips_empty_fact_value(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_client,
        "simple_completion",
        lambda *_args, **_kwargs: SimpleNamespace(
            text='[{"entity_id":"workspace_1","entity_type":"workspace","fact_key":"stack","fact_value":""}]'
        ),
    )
    monkeypatch.setattr(fact_extractor, "upsert_fact", lambda *args, **kwargs: calls.append((args, kwargs)))

    stored = fact_extractor.extract_and_store_facts("user_1", "conv_1", "Synthesis", db="db")

    assert stored == 0
    assert calls == []


def test_extract_and_store_facts_upsert_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        model_client,
        "simple_completion",
        lambda *_args, **_kwargs: SimpleNamespace(
            text='[{"entity_id":"workspace_1","entity_type":"workspace","fact_key":"stack","fact_value":"Postgres"}]'
        ),
    )

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("db failed")

    monkeypatch.setattr(fact_extractor, "upsert_fact", fail_upsert)

    assert fact_extractor.extract_and_store_facts("user_1", "conv_1", "Synthesis", db="db") == 0
