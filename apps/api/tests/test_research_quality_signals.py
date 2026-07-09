from __future__ import annotations

from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import OrchestratorDecision
from app.services.agent.research_models import CitationVerification, EvidenceClaim, EvidenceItem, EvidencePack
from app.services.agent.runtime import Runtime

from test_agent_runtime import FakeTools, _patch_completion


def _runtime() -> Runtime:
    return Runtime(tools=FakeTools())


def test_quality_signals_none_when_no_evidence_or_citation_result():
    signals = _runtime()._research_quality_signals({})

    assert signals is None


def test_quality_signals_none_when_evidence_pack_is_empty_and_no_citation_result():
    signals = _runtime()._research_quality_signals({"evidence": EvidencePack()})

    assert signals is None


def test_quality_signals_populated_from_evidence_and_citation_result():
    evidence = EvidencePack(
        items=[EvidenceItem(source_id="S1", title="A", url="https://example.com/a", evidence="text")],
        claims=[
            EvidenceClaim(source_id="S1", text="claim one", staleness="current"),
            EvidenceClaim(source_id="S1", text="claim two", staleness="stale"),
        ],
        coverage=0.75,
    )
    citation_result = CitationVerification(verified_claims=3, unsupported_claims=["claim x"])

    signals = _runtime()._research_quality_signals(
        {
            "evidence": evidence,
            "langgraph_state": {"last_citation_verification": citation_result},
        }
    )

    assert signals is not None
    assert signals.coverage_ratio == 0.75
    assert signals.verified_claim_count == 3
    assert signals.unsupported_claim_count == 1
    assert signals.has_stale_evidence is True


def test_quality_signals_no_stale_evidence_when_all_claims_current():
    evidence = EvidencePack(
        items=[EvidenceItem(source_id="S1", title="A", url="https://example.com/a", evidence="text")],
        claims=[EvidenceClaim(source_id="S1", text="claim one", staleness="current")],
        coverage=1.0,
    )

    signals = _runtime()._research_quality_signals({"evidence": evidence})

    assert signals is not None
    assert signals.has_stale_evidence is False
    assert signals.verified_claim_count == 0
    assert signals.unsupported_claim_count == 0


def test_research_route_stream_surfaces_quality_signals_on_turn_result(monkeypatch):
    _patch_completion(monkeypatch, text="Final answer with a claim [S1].")
    from app.services.agent import runtime as runtime_module
    import app.services.agent.langgraph_runtime.nodes as nodes_module

    monkeypatch.setattr(runtime_module, "decide_fast_path", lambda request: type("FastPath", (), {"path": "none"})())
    monkeypatch.setattr(
        runtime_module,
        "decide_with_options",
        lambda request, **kwargs: OrchestratorDecision(
            route="research",
            research_level="easy",
            requires_confirmation=False,
            reason="test",
            source="test",
            available_routes=kwargs.get("available_routes", []),
            available_tools=kwargs.get("available_tools", []),
        ),
    )

    original_verify = nodes_module.verify

    def inject_citation_result(state, *, run_id, request, tools=None, progress=None):
        result = original_verify(state, run_id=run_id, request=request, tools=tools, progress=progress)
        result["last_citation_verification"] = CitationVerification(
            verified_claims=2,
            unsupported_claims=["unsupported claim"],
        )
        return result

    monkeypatch.setattr(nodes_module, "verify", inject_citation_result)

    envelopes = list(
        Runtime(tools=FakeTools()).run_stream(
            TurnRequest(message="Research current AI governance trends.", research_level="easy"),
            user_id="u1",
        )
    )

    result = next(envelope.data for envelope in envelopes if envelope.type == "result")

    assert result["quality_signals"] is not None
    assert result["quality_signals"]["verified_claim_count"] == 2
    assert result["quality_signals"]["unsupported_claim_count"] == 1
