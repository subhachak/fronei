"""Tests for Phase 1 — LLM-backed claim classification in research_evidence.py.

Verifies:
  - classify_claims_llm returns operational_reality for practitioner timing reports.
  - classify_claims_llm returns official_policy for authoritative regulatory text.
  - On model failure, falls back to regex without raising.
  - Budget ledger exhaustion causes immediate regex fallback (no model call).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.agent.research_evidence import classify_claims_llm
from app.services.agent.research_models import EvidenceItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_item(source_type: str = "web", url: str = "https://reddit.com/r/immigration/comments/test", title: str = "Test") -> EvidenceItem:
    return EvidenceItem(
        source_id="S1",
        question="How long does H-4 EAD take?",
        title=title,
        url=url,
        source_type=source_type,
        evidence="test content",
        relevance=0.8,
        confidence=0.7,
        authority=0.52,
    )


def _mock_response(classifications: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        text=json.dumps({"classifications": classifications}),
        cost_usd=0.001,
        latency_ms=300,
    )


# ---------------------------------------------------------------------------
# Core classification tests
# ---------------------------------------------------------------------------

class TestClassifyClaimsLlm:
    """classify_claims_llm should return LLM-assigned roles when the model succeeds."""

    def test_operational_reality_for_practitioner_timing(self):
        """A first-person timing report from a forum should become operational_reality."""
        sentence = "I got my EAD in 4 months, filed concurrently with H-1B renewal per r/immigration."
        item = _make_item(source_type="web", url="https://reddit.com/r/immigration/comments/abc")

        mock_resp = _mock_response([
            {"claim_type": "timeline", "claim_role": "operational_reality", "freshness_risk": "high"}
        ])

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.return_value = mock_resp

            results = classify_claims_llm([sentence], item)

        assert len(results) == 1
        assert results[0]["claim_role"] == "operational_reality", (
            f"Expected operational_reality, got: {results[0]['claim_role']}"
        )
        assert results[0]["claim_type"] == "timeline"

    def test_official_policy_for_regulatory_text(self):
        """Regulatory rule text from a .gov source should become official_policy."""
        sentence = "USCIS requires Form I-765 to be filed with a valid I-94 and evidence of H-4 status."
        item = _make_item(source_type="government", url="https://uscis.gov/i-765")

        mock_resp = _mock_response([
            {"claim_type": "policy", "claim_role": "official_policy", "freshness_risk": "low"}
        ])

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.return_value = mock_resp

            results = classify_claims_llm([sentence], item)

        assert results[0]["claim_role"] == "official_policy"
        assert results[0]["claim_type"] == "policy"

    def test_anecdotal_case_for_personal_experience(self):
        """A personal account should become anecdotal_case."""
        sentence = "My case was approved in 6 weeks after the biometrics appointment was waived."
        item = _make_item(source_type="web", url="https://immigrationforums.net/post/123")

        mock_resp = _mock_response([
            {"claim_type": "anecdote", "claim_role": "anecdotal_case", "freshness_risk": "medium"}
        ])

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.return_value = mock_resp

            results = classify_claims_llm([sentence], item)

        assert results[0]["claim_role"] == "anecdotal_case"

    def test_batch_returns_one_result_per_sentence(self):
        """Result list length must equal input sentence count."""
        sentences = [
            "I waited 5 months for my EAD.",
            "USCIS officially targets 3-5 months.",
            "Attorneys advise filing early.",
        ]
        item = _make_item()

        mock_resp = _mock_response([
            {"claim_type": "anecdote", "claim_role": "anecdotal_case", "freshness_risk": "high"},
            {"claim_type": "timeline", "claim_role": "official_policy", "freshness_risk": "medium"},
            {"claim_type": "interpretation", "claim_role": "expert_interpretation", "freshness_risk": "low"},
        ])

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.return_value = mock_resp

            results = classify_claims_llm(sentences, item)

        assert len(results) == 3
        assert results[0]["claim_role"] == "anecdotal_case"
        assert results[1]["claim_role"] == "official_policy"
        assert results[2]["claim_role"] == "expert_interpretation"


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------

class TestClassifyClaimsLlmFallback:
    """On model failure, classify_claims_llm must fall back to regex without raising."""

    def test_falls_back_to_regex_on_model_exception(self):
        """If model_client.complete raises, result should come from regex, not error."""
        sentence = "According to the USCIS attorney the wait time is currently 6 months."
        item = _make_item(source_type="web")

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.side_effect = RuntimeError("model unavailable")

            results = classify_claims_llm([sentence], item)

        assert len(results) == 1
        # Regex fallback should still return a valid role — not crash.
        assert results[0]["claim_role"] in {
            "official_policy", "operational_reality", "expert_interpretation",
            "anecdotal_case", "statistical_data", "technical_design",
            "implementation_detail", "background_context",
        }

    def test_falls_back_to_regex_on_json_parse_error(self):
        """If the model returns malformed JSON, result should come from regex."""
        sentence = "My EAD took 8 months."
        item = _make_item(source_type="web")

        bad_resp = SimpleNamespace(text="not valid json {{", cost_usd=0.001, latency_ms=200)

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.return_value = bad_resp

            results = classify_claims_llm([sentence], item)

        assert len(results) == 1
        assert "claim_role" in results[0]
        assert "claim_type" in results[0]

    def test_falls_back_when_budget_exhausted(self):
        """If ledger.can_start_model returns False, model must NOT be called."""
        sentence = "Processing times are currently 4 months."
        item = _make_item()

        mock_ledger = MagicMock()
        mock_ledger.can_start_model.return_value = False

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")

            results = classify_claims_llm([sentence], item, ledger=mock_ledger)

        mock_client.complete.assert_not_called()
        assert len(results) == 1

    def test_empty_sentences_returns_empty(self):
        """Empty sentence list returns empty list without any LLM call."""
        item = _make_item()
        with patch("app.services.agent.research_evidence.model_client") as mock_client:
            results = classify_claims_llm([], item)
        mock_client.complete.assert_not_called()
        assert results == []

    def test_unknown_role_from_llm_falls_back_to_regex(self):
        """If the LLM returns an unrecognised role, the regex value is used instead."""
        sentence = "The processing time is currently 5 months based on community reports."
        item = _make_item(source_type="web")

        mock_resp = _mock_response([
            {"claim_type": "timeline", "claim_role": "INVALID_ROLE", "freshness_risk": "high"}
        ])

        with patch("app.services.agent.research_evidence.model_client") as mock_client, \
             patch("app.services.agent.research_evidence.resolve_prompt") as mock_resolve:
            mock_resolve.return_value = SimpleNamespace(system_prompt="{}")
            mock_client.complete.return_value = mock_resp

            results = classify_claims_llm([sentence], item)

        valid_roles = {
            "official_policy", "operational_reality", "expert_interpretation",
            "anecdotal_case", "statistical_data", "technical_design",
            "implementation_detail", "background_context",
        }
        assert results[0]["claim_role"] in valid_roles
        # claim_type from LLM was valid ("timeline") so should be kept
        assert results[0]["claim_type"] == "timeline"
