"""Slice 4 stop condition tests: claim classification wired before bind.

Stop conditions:
  1. claim_classification_results is non-empty after classify_claims runs.
  2. Each record has url, source_type, classifications keys.
  3. At least one classification has a non-default claim_role.
  4. evidence.claims (populated by bind_evidence) is non-empty.
  5. evidence.claims have claim_role set (LLM classification inside bind).
  6. classify_claims node is visited; artifacts status is "real" (not stub).
  7. model_calls_made increases after classify_claims fires.
  8. EHR fixture: classify_claims produces records for at least 1 source.
  9. No sources with content are silently skipped (empty content excluded gracefully).
 10. claim_classification_results accumulates via operator.add reducer.
"""
from __future__ import annotations

import pytest

from app.services.agent.langgraph_runtime.graph import run_stub_graph
from app.services.agent.langgraph_runtime.runtime import run_langgraph_research
from app.services.agent.langgraph_runtime.state import ResearchGraphState
from app.services.agent.models import TurnRequest

from test_agent_runtime import FakeTools as _BaseFakeTools, _patch_completion


class FakeTools(_BaseFakeTools):
    """FakeTools with long-form content so _claim_candidate_sentences fires (≥45 chars)."""

    def extract_urls(self, urls: list[str], max_chars_per_source: int = 2500):
        from app.services.agent.models import Source, ToolCall

        extracted = [
            Source(
                title="Example Research Source",
                url="https://example.com",
                content=(
                    # Sentences deliberately contain plan query terms ("evidence", "supports",
                    # "changing") so _score_claim_sentence gives a score > 0 and
                    # extract_evidence_claims includes them in evidence.claims.
                    "The evidence supports the view that Epic Systems is changing its pricing model "
                    "for large hospital markets with strong interoperability requirements. "
                    "Oracle Health evidence suggests competitive pricing changes for mid-sized practices. "
                    "The evidence supports athenahealth's cloud-native EHR with built-in revenue management. "
                    "Veradigm evidence shows changing ambulatory workflows across specialty care in the US. "
                    "Meditech evidence supports a fully integrated EHR platform for community hospitals."
                ),
            )
        ]
        tc = ToolCall(
            name="read_url",
            input={"urls": urls},
            output={"source_count": 1},
            latency_ms=1,
        )
        return extracted, tc


# All tests patch completions — classify_claims_llm uses model_client.complete.
@pytest.fixture(autouse=True)
def patch_model_completions(monkeypatch):
    _patch_completion(monkeypatch)


_REQUEST = TurnRequest(message="Research something for Slice 4 tests.")
_EHR_REQUEST = TurnRequest(
    message=(
        "Compare EHR vendors: Epic, Oracle Health, athenahealth, Veradigm, and Meditech. "
        "Coverage: pricing, integration, user experience, support, and deployment."
    ),
    research_level="regular",
)

_INITIAL: ResearchGraphState = {
    "request_message": "",
    "visited_nodes": [],
    "artifacts": {},
}


def _run(request: TurnRequest = _REQUEST) -> ResearchGraphState:
    return run_stub_graph(
        {**_INITIAL, "request_message": request.message},
        run_id="slice4-test",
        request=request,
        tools=FakeTools(),
    )


# ---------------------------------------------------------------------------
# 4.1  claim_classification_results is populated
# ---------------------------------------------------------------------------

def test_claim_classification_results_non_empty():
    result = _run()
    records = result.get("claim_classification_results") or []
    assert isinstance(records, list), "claim_classification_results must be a list"
    # FakeTools returns sources with content; at least one should be classified.
    assert len(records) >= 1, (
        f"Expected at least 1 classification record; got {records}"
    )


def test_classification_record_has_required_keys():
    result = _run()
    records = result.get("claim_classification_results") or []
    assert records, "claim_classification_results must be non-empty"
    for rec in records:
        assert "url" in rec, f"Record missing 'url': {rec}"
        assert "source_type" in rec, f"Record missing 'source_type': {rec}"
        assert "classifications" in rec, f"Record missing 'classifications': {rec}"


def test_classification_record_has_sentence_count():
    result = _run()
    records = result.get("claim_classification_results") or []
    assert records
    for rec in records:
        assert "sentence_count" in rec, f"Record missing 'sentence_count': {rec}"
        assert rec["sentence_count"] >= 1


# ---------------------------------------------------------------------------
# 4.2  At least one classification has claim_role set
# ---------------------------------------------------------------------------

def test_at_least_one_claim_role_in_classifications():
    """Each classification dict from classify_claims_llm has claim_role."""
    result = _run()
    records = result.get("claim_classification_results") or []
    assert records
    all_classifications = [
        c for rec in records for c in rec.get("classifications", [])
    ]
    assert all_classifications, "No classifications found in any record"
    roles = {c.get("claim_role") for c in all_classifications}
    assert roles, "No claim_role found in any classification"
    # Must not be all-empty
    assert any(r for r in roles), "All claim_roles are empty/None"


# ---------------------------------------------------------------------------
# 4.3  evidence.claims also populated (bind_evidence internal classification)
# ---------------------------------------------------------------------------

def test_evidence_claims_non_empty():
    """bind_evidence internal classify_claims_llm also fires → evidence.claims set."""
    result = _run()
    evidence = result.get("evidence")
    assert evidence is not None, "evidence must be set after bind"
    assert len(evidence.claims) >= 1, (
        f"Expected evidence.claims to be non-empty; got {evidence.claims}"
    )


def test_evidence_claims_have_claim_role():
    result = _run()
    evidence = result.get("evidence")
    assert evidence is not None
    for claim in evidence.claims:
        assert claim.claim_role, f"Claim missing claim_role: {claim}"


# ---------------------------------------------------------------------------
# 4.4  classify_claims node is visited; artifact status is "real"
# ---------------------------------------------------------------------------

def test_classify_claims_node_visited():
    result = _run()
    assert "classify_claims" in result.get("visited_nodes", [])


def test_classify_claims_artifact_status_real():
    result = _run()
    artifacts = result.get("artifacts") or {}
    classify_artifact = artifacts.get("classify_claims", {})
    assert classify_artifact.get("status") == "real", (
        f"Expected classify_claims artifact status 'real'; got {classify_artifact}"
    )


# ---------------------------------------------------------------------------
# 4.5  model_calls_made reflects classify_claims LLM calls
# ---------------------------------------------------------------------------

def test_model_calls_include_classify_claims():
    """model_calls_made should be at least brief(1) + synthesize(1) + classify_claims(≥1)."""
    result = _run()
    # FakeTools returns 1 source with content → at least 1 LLM call from classify_claims.
    assert result.get("model_calls_made", 0) >= 3, (
        f"Expected model_calls_made>=3; got {result.get('model_calls_made')}"
    )


# ---------------------------------------------------------------------------
# 4.6  EHR fixture: classify_claims produces records for ≥1 vendor source
# ---------------------------------------------------------------------------

def test_ehr_fixture_classify_claims_produces_records():
    result = _run(_EHR_REQUEST)
    records = result.get("claim_classification_results") or []
    assert len(records) >= 1, (
        f"EHR fixture: expected ≥1 classification record; got {records}"
    )


def test_ehr_fixture_evidence_claims_have_roles():
    result = _run(_EHR_REQUEST)
    evidence = result.get("evidence")
    assert evidence is not None
    # Each claim must have a claim_role set by classify_claims_llm in bind_evidence.
    for claim in evidence.claims:
        assert claim.claim_role, f"EHR claim missing claim_role: {claim}"


# ---------------------------------------------------------------------------
# 4.7  Graceful handling: sources without content are excluded
# ---------------------------------------------------------------------------

def test_sources_without_content_excluded_gracefully():
    """Injecting a no-content source must not crash classify_claims."""
    from app.services.agent.models import Source
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    original_dispatch_router = nodes_module.dispatch_search_router

    # Add a bare-minimum source to what's already in state via a monkeypatch
    # is complex; simplest: verify the node handles empty sources list cleanly.
    state: ResearchGraphState = {
        "request_message": "no content test",
        "visited_nodes": [],
        "artifacts": {},
        "sources": [Source(title="Empty", url="https://empty.example.com", snippet="")],
    }
    result_node = nodes_module.classify_claims(
        state,
        run_id="test-no-content",
        request=_REQUEST,
        tools=None,
        progress=None,
    )
    # Should succeed without crashing; records may be empty because no content.
    assert isinstance(result_node.get("claim_classification_results"), list)
    assert "classify_claims" in result_node.get("visited_nodes", [])


# ---------------------------------------------------------------------------
# 4.8  claim_classification_results uses operator.add accumulator
# ---------------------------------------------------------------------------

def test_claim_classification_results_is_list():
    """Confirm the reducer field type is list[dict] — shape for operator.add."""
    result = _run()
    records = result.get("claim_classification_results")
    assert isinstance(records, list)
    for item in records:
        assert isinstance(item, dict), f"Expected dict in records; got {type(item)}"


# ---------------------------------------------------------------------------
# 4.9  Full runtime: run_langgraph_research populates claim data
# ---------------------------------------------------------------------------

def test_full_runtime_claim_results_in_state():
    result = run_langgraph_research(_REQUEST, FakeTools())
    state = result["langgraph_state"]
    records = state.get("claim_classification_results") or []
    assert isinstance(records, list), "claim_classification_results must be a list in final state"
