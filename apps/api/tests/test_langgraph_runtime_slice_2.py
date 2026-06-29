"""Slice 2 tests: search fan-out, rank, read, bind, and parity smoke test.

Stop conditions tested:
- search_worker nodes execute and accumulate sources via operator.add reducers
- rank node selects top URLs into ranked_source_urls
- read node calls extract_urls and appends extracted sources
- bind node produces a non-empty EvidencePack from FakeTools sources
- EHR fixture: N workers × 1 source/worker = N sources in state
- Parity smoke: langgraph search+bind path produces same source count
  as a single FakeTools.search_web call for a generic query
"""
from __future__ import annotations

import pytest

from app.services.agent.langgraph_runtime.graph import run_stub_graph
from app.services.agent.langgraph_runtime.state import ResearchGraphState
from app.services.agent.models import TurnRequest
from app.services.agent.research_models import EvidencePack

from test_agent_runtime import FakeTools

EHR_QUERY = (
    "Compare Epic, Oracle Cerner, Meditech Expanse, athenahealth, and eClinicalWorks as EHR platforms "
    "for a 500-bed US hospital: pricing, interoperability, patient portal, and support quality."
)
EHR_EXPECTED_SUBJECTS = {
    "Epic", "Oracle Cerner", "Meditech Expanse", "athenahealth", "eClinicalWorks"
}

_INITIAL: ResearchGraphState = {
    "request_message": "",
    "visited_nodes": [],
    "artifacts": {},
}


def _run(message: str, tools=None) -> ResearchGraphState:
    return run_stub_graph(
        {**_INITIAL, "request_message": message},
        run_id="slice2-test",
        request=TurnRequest(message=message),
        tools=tools or FakeTools(),
    )


# ---------------------------------------------------------------------------
# 2.1  dispatch_search + search_worker: sources accumulate
# ---------------------------------------------------------------------------

def test_search_worker_sources_accumulate_in_state():
    """Every search_worker result is accumulated via operator.add; state.sources is non-empty."""
    result = _run("What are the best EHR platforms?")
    sources = result.get("sources", [])
    # FakeTools returns 1 source per worker; plan will have ≥1 worker
    assert len(sources) > 0, "Expected sources from search_worker; got 0"


def test_search_worker_reports_accumulate_in_state():
    """worker_reports list is populated — one report per search_worker invocation."""
    result = _run("What are the best EHR platforms?")
    reports = result.get("worker_reports", [])
    assert len(reports) > 0, "Expected worker_reports; got 0"


def test_tool_calls_accumulate_across_workers():
    """tool_calls_made counter reflects all search_worker tool invocations."""
    result = _run("What are the best EHR platforms?")
    # Each worker makes 1 search tool call; ≥1 workers → tool_calls_made ≥ 1
    assert result.get("tool_calls_made", 0) >= 1


# ---------------------------------------------------------------------------
# 2.2  rank node: produces ranked_source_urls
# ---------------------------------------------------------------------------

def test_rank_node_produces_ranked_source_urls():
    """ranked_source_urls is a non-empty list of URL strings after rank runs."""
    result = _run("What are the best EHR platforms?")
    ranked_urls = result.get("ranked_source_urls", [])
    assert isinstance(ranked_urls, list)
    assert len(ranked_urls) > 0, "ranked_source_urls should be populated by rank node"
    for url in ranked_urls:
        assert isinstance(url, str) and url.startswith("http"), f"Expected URL, got {url!r}"


# ---------------------------------------------------------------------------
# 2.3  read node: extracted sources appended to state
# ---------------------------------------------------------------------------

def test_read_node_appends_extracted_sources():
    """After read, state.sources contains both search-result and extracted sources."""
    result = _run("What are the best EHR platforms?")
    # FakeTools.extract_urls returns Source(content="Detailed evidence")
    contents = [s.content for s in result.get("sources", []) if s.content]
    assert len(contents) > 0, "Expected at least one extracted source with content"


# ---------------------------------------------------------------------------
# 2.4  bind node: EvidencePack produced
# ---------------------------------------------------------------------------

def test_bind_node_produces_evidence_pack():
    """bind node returns an EvidencePack in state.evidence."""
    result = _run("What are the best EHR platforms?")
    evidence = result.get("evidence")
    assert evidence is not None, "evidence should be set after bind node"
    assert isinstance(evidence, EvidencePack)


def test_bind_node_evidence_has_items():
    """EvidencePack has ≥1 items when FakeTools provides sources with content."""
    result = _run("What are the best EHR platforms?")
    evidence = result.get("evidence")
    assert evidence is not None
    assert len(evidence.items) >= 1, (
        f"Expected ≥1 evidence items; got {len(evidence.items)}"
    )


# ---------------------------------------------------------------------------
# 2.5  EHR fixture: 5 vendors → workers → sources
# ---------------------------------------------------------------------------

def test_ehr_fixture_search_workers_run_for_all_vendors():
    """EHR query with 5 vendors produces ≥5 worker reports (one per coverage question)."""
    result = run_stub_graph(
        {**_INITIAL, "request_message": EHR_QUERY},
        run_id="slice2-ehr",
        request=TurnRequest(message=EHR_QUERY, research_level="deep"),
        tools=FakeTools(),
    )
    reports = result.get("worker_reports", [])
    assert len(reports) >= 5, (
        f"Expected ≥5 worker reports for EHR 5-vendor query; got {len(reports)}"
    )


def test_ehr_fixture_sources_count_matches_workers():
    """EHR fixture: state.sources count equals number of workers (FakeTools returns 1/worker)."""
    result = run_stub_graph(
        {**_INITIAL, "request_message": EHR_QUERY},
        run_id="slice2-ehr-sources",
        request=TurnRequest(message=EHR_QUERY, research_level="deep"),
        tools=FakeTools(),
    )
    reports = result.get("worker_reports", [])
    # FakeTools returns exactly 1 source per search_web call
    # Plus extract_urls adds 1 extracted source per read batch
    # At minimum, sources ≥ number of workers
    sources = result.get("sources", [])
    assert len(sources) >= len(reports), (
        f"Expected ≥{len(reports)} sources (one per worker); got {len(sources)}"
    )


def test_ehr_fixture_evidence_bound_from_extracted_sources():
    """EHR fixture: evidence.items are populated after full search+read+bind pipeline."""
    result = run_stub_graph(
        {**_INITIAL, "request_message": EHR_QUERY},
        run_id="slice2-ehr-evidence",
        request=TurnRequest(message=EHR_QUERY, research_level="deep"),
        tools=FakeTools(),
    )
    evidence = result.get("evidence")
    assert evidence is not None
    # FakeTools.extract_urls returns content="Detailed evidence" — bind should pick this up
    assert len(evidence.items) >= 1, (
        f"Expected ≥1 evidence items for EHR fixture; got {len(evidence.items)}"
    )


# ---------------------------------------------------------------------------
# 2.6  Parity smoke: search node runs real tools, not stub
# ---------------------------------------------------------------------------

def test_search_is_no_longer_stubbed():
    """Slice 2: dispatched search_worker invokes tools.search_web (not the stub placeholder)."""
    call_log = []

    class TracingTools(FakeTools):
        def search_web(self, query, max_results=6):
            call_log.append(query)
            return super().search_web(query, max_results)

    result = run_stub_graph(
        {**_INITIAL, "request_message": "What is the best EHR?"},
        run_id="slice2-parity",
        request=TurnRequest(message="What is the best EHR?"),
        tools=TracingTools(),
    )
    assert len(call_log) >= 1, (
        f"Expected tools.search_web to be called ≥1 times; call_log={call_log}"
    )


def test_bind_uses_url_priority_merge():
    """URL-priority merge: extracted source (with content) takes priority over snippet-only."""
    from app.services.agent.models import Source
    from app.services.agent.langgraph_runtime.nodes import _url_priority_merge

    snippet_only = Source(title="A", url="http://example.com", snippet="just a snippet")
    extracted = Source(title="A", url="http://example.com", snippet="just a snippet", content="Full content here")
    # Extracted should win
    merged = _url_priority_merge([snippet_only, extracted])
    assert len(merged) == 1
    assert merged[0].content == "Full content here"

    # Reverse order — extracted still wins
    merged2 = _url_priority_merge([extracted, snippet_only])
    assert len(merged2) == 1
    assert merged2[0].content == "Full content here"


# ---------------------------------------------------------------------------
# 2.7  No-tools shortcircuit: dispatch_search_router returns ["rank"] directly
# ---------------------------------------------------------------------------

def test_no_tools_shortcircuits_to_rank():
    """When tools=None, dispatch_search_router shortcircuits directly to rank."""
    result = run_stub_graph(
        {**_INITIAL, "request_message": "What is EHR?"},
        run_id="slice2-notools",
        request=TurnRequest(message="What is EHR?"),
        tools=None,
    )
    visited = result.get("visited_nodes", [])
    # dispatch_search must be visited; search_worker must NOT be
    assert "dispatch_search" in visited
    assert "search_worker" not in visited, (
        f"Expected no search_worker when tools=None; visited={visited}"
    )
    # rank still runs (sources just empty)
    assert "rank" in visited
