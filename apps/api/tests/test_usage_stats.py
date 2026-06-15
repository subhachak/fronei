"""Tests for Phase 3 usage-stats logging and ranking weight changes (#128-130
of agentdeck_framework_architecture.md §3/§6):

  - `log_doc_plan_usage` upserts `component_usage_stats` rows from a `DocPlan` (#128)
  - `log_render_qa_failures` increments `failure_count` for components on
    QA-flagged slides (#129)
  - `load_usage_stats_map` + `selection.rank_components`/`score_component`
    weight candidate ordering by real success_rate (#130)
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, ComponentUsageStat
from app.services.components import (
    ContentBlock,
    DocPlan,
    SectionPlan,
    load_usage_stats_map,
    log_doc_plan_usage,
    log_render_qa_failures,
)
from app.services.components.selection import rank_components, score_component
from app.services.components.registry import get_component


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _sample_doc_plan() -> DocPlan:
    return DocPlan(
        title="Strategy Review",
        sections=[
            SectionPlan(
                slide_layout="CONTENT_1COL",
                section_title="Adoption",
                blocks=[
                    ContentBlock(
                        zone="body",
                        component_id="bullet_list",
                        data={"items": [{"text": "Point A", "level": 0}]},
                    )
                ],
            ),
            SectionPlan(
                slide_layout="CONTENT_2COL",
                section_title="Comparison",
                blocks=[
                    ContentBlock(zone="col_left", component_id="card", data={"title": "Option A", "body": "Summary"}),
                    ContentBlock(
                        zone="col_right",
                        component_id="bullet_list",
                        data={"items": [{"text": "Point B", "level": 0}]},
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# log_doc_plan_usage (#128)
# ---------------------------------------------------------------------------


def test_log_doc_plan_usage_inserts_rows(db_session):
    doc_plan = _sample_doc_plan()
    log_doc_plan_usage(db_session, "presentation", doc_plan.model_dump_json())

    rows = db_session.query(ComponentUsageStat).all()
    by_key = {(r.component_id, r.slide_layout): r for r in rows}

    assert by_key[("bullet_list", "CONTENT_1COL")].success_count == 1
    assert by_key[("card", "CONTENT_2COL")].success_count == 1
    assert by_key[("bullet_list", "CONTENT_2COL")].success_count == 1
    # TITLE has no blocks -> no row.
    assert ("hero_title", "TITLE") not in by_key
    for row in rows:
        assert row.design_system == "agentdeck_v1"
        assert row.theme == "dark"
        assert row.failure_count == 0
        assert row.last_used_at is not None


def test_log_doc_plan_usage_is_idempotent_upsert(db_session):
    doc_plan = _sample_doc_plan()
    body = doc_plan.model_dump_json()
    log_doc_plan_usage(db_session, "presentation", body)
    log_doc_plan_usage(db_session, "presentation", body)

    rows = db_session.query(ComponentUsageStat).all()
    by_key = {(r.component_id, r.slide_layout): r for r in rows}
    assert by_key[("bullet_list", "CONTENT_1COL")].success_count == 2
    assert by_key[("card", "CONTENT_2COL")].success_count == 2


def test_log_doc_plan_usage_noop_for_non_presentation(db_session):
    doc_plan = _sample_doc_plan()
    log_doc_plan_usage(db_session, "executive_report", doc_plan.model_dump_json())
    assert db_session.query(ComponentUsageStat).count() == 0


def test_log_doc_plan_usage_noop_for_legacy_deckplan_json(db_session):
    legacy = '{"title": "Deck", "slides": [{"layout": "bullets", "title": "X", "bullets": ["a"]}]}'
    log_doc_plan_usage(db_session, "presentation", legacy)
    assert db_session.query(ComponentUsageStat).count() == 0


# ---------------------------------------------------------------------------
# log_render_qa_failures (#129)
# ---------------------------------------------------------------------------


def test_log_render_qa_failures_increments_failure_count_for_flagged_slide(db_session):
    doc_plan = _sample_doc_plan()
    body = doc_plan.model_dump_json()

    # slide 1 is the synthesized TITLE slide.
    # doc_plan.sections[0] (CONTENT_1COL, bullet_list) -> renders as slide 2.
    # doc_plan.sections[1] (CONTENT_2COL, card + bullet_list) -> renders as slide 3.
    render_qa = {
        "available": True,
        "issues": [{"slide": 2, "type": "dense_text", "detail": "too much text"}],
    }
    log_render_qa_failures(db_session, "presentation", body, render_qa)

    rows = {(r.component_id, r.slide_layout): r for r in db_session.query(ComponentUsageStat).all()}
    assert rows[("bullet_list", "CONTENT_1COL")].failure_count == 1
    assert rows[("bullet_list", "CONTENT_1COL")].success_count == 0
    # Slide 3's (sections[1]) components are untouched.
    assert ("card", "CONTENT_2COL") not in rows
    assert ("bullet_list", "CONTENT_2COL") not in rows


def test_log_render_qa_failures_ignores_non_failure_issue_types(db_session):
    doc_plan = _sample_doc_plan()
    body = doc_plan.model_dump_json()
    render_qa = {"available": True, "issues": [{"slide": 2, "type": "blank", "detail": "blank slide"}]}
    log_render_qa_failures(db_session, "presentation", body, render_qa)
    assert db_session.query(ComponentUsageStat).count() == 0


def test_log_render_qa_failures_noop_when_no_issues(db_session):
    doc_plan = _sample_doc_plan()
    log_render_qa_failures(db_session, "presentation", doc_plan.model_dump_json(), {"available": True, "issues": []})
    log_render_qa_failures(db_session, "presentation", doc_plan.model_dump_json(), None)
    assert db_session.query(ComponentUsageStat).count() == 0


def test_log_render_qa_failures_combines_with_success_logging(db_session):
    doc_plan = _sample_doc_plan()
    body = doc_plan.model_dump_json()
    log_doc_plan_usage(db_session, "presentation", body)
    log_render_qa_failures(
        db_session, "presentation", body,
        {"available": True, "issues": [{"slide": 3, "type": "dense_ink", "detail": "crowded"}]},
    )

    rows = {(r.component_id, r.slide_layout): r for r in db_session.query(ComponentUsageStat).all()}
    card_row = rows[("card", "CONTENT_2COL")]
    assert card_row.success_count == 1
    assert card_row.failure_count == 1
    # Slide 2's (sections[0]) bullet_list wasn't flagged.
    assert rows[("bullet_list", "CONTENT_1COL")].failure_count == 0


# ---------------------------------------------------------------------------
# load_usage_stats_map + ranking weight (#130)
# ---------------------------------------------------------------------------


def test_load_usage_stats_map_returns_empty_for_no_db():
    assert load_usage_stats_map(None) == {}


def test_load_usage_stats_map_computes_success_rate(db_session):
    db_session.add(ComponentUsageStat(
        component_id="table",
        slide_layout="CONTENT_1COL",
        design_system="agentdeck_v1",
        theme="dark",
        success_count=1,
        failure_count=3,
    ))
    db_session.add(ComponentUsageStat(
        component_id="bullet_list",
        slide_layout="CONTENT_1COL",
        design_system="agentdeck_v1",
        theme="dark",
        success_count=9,
        failure_count=1,
    ))
    db_session.commit()

    usage_map = load_usage_stats_map(db_session)
    assert usage_map[("table", "CONTENT_1COL", "agentdeck_v1", "dark")] == pytest.approx(0.25)
    assert usage_map[("bullet_list", "CONTENT_1COL", "agentdeck_v1", "dark")] == pytest.approx(0.9)


def test_score_component_uses_override_success_rate():
    table = get_component("table")
    score_neutral = score_component(table, {"comparison"})
    score_low = score_component(table, {"comparison"}, success_rate=0.0)
    score_high = score_component(table, {"comparison"}, success_rate=1.0)
    assert score_low < score_neutral < score_high


def test_rank_components_reorders_via_usage_stats_map():
    # With no tags, both `table` and `bullet_list` start tied at the neutral
    # success_rate (0.5) for CONTENT_1COL -> order is registry-stable.
    baseline = rank_components("CONTENT_1COL", [])
    baseline_ids = [c.id for c in baseline]
    assert "table" in baseline_ids and "bullet_list" in baseline_ids

    # A usage_stats_map that penalizes `table` heavily and rewards
    # `bullet_list` should push bullet_list ahead of table even though
    # bullet_list ranked behind table at baseline.
    usage_stats_map = {
        ("table", "CONTENT_1COL", "agentdeck_v1", "dark"): 0.0,
        ("bullet_list", "CONTENT_1COL", "agentdeck_v1", "dark"): 1.0,
    }
    ranked = rank_components("CONTENT_1COL", [], usage_stats_map=usage_stats_map)
    ranked_ids = [c.id for c in ranked]
    assert ranked_ids.index("bullet_list") < ranked_ids.index("table")


def test_rank_components_falls_back_to_neutral_for_unknown_keys():
    # An empty/irrelevant usage_stats_map should behave identically to None.
    no_map = rank_components("CONTENT_1COL", ["comparison"])
    empty_map = rank_components("CONTENT_1COL", ["comparison"], usage_stats_map={})
    assert [c.id for c in no_map] == [c.id for c in empty_map]
