from app.services.presentation_design_system import (
    FIT_CONTRACTS,
    PRIMITIVE_COMPONENTS,
    SLIDE_TEMPLATES,
    canonical_layout,
    component_tree_for_slide,
    design_system_payload,
)


def test_design_system_payload_contains_tokens_templates_and_primitives():
    payload = design_system_payload("data-product-os")

    assert payload["name"] == "fronei_pptx_design_system"
    assert payload["version"] == 1
    assert payload["tokens"]["theme"]["accent"] == "34D399"
    assert "chart_palette" in payload["tokens"]["theme"]
    assert "risk_matrix" in payload["templates"]
    assert "RiskMatrix" in payload["primitive_components"]
    assert FIT_CONTRACTS["Table"]["max_columns"] == 5
    assert "ArchitectureDiagram" in PRIMITIVE_COMPONENTS


def test_canonical_layout_aliases_and_unknown_fallback():
    assert canonical_layout("section_break") == ("section", None)
    assert canonical_layout("toc") == ("agenda", None)
    assert canonical_layout("quote") == ("callout", None)
    assert canonical_layout("thank_you") == ("recommendation", None)
    assert canonical_layout("decision_pack_cover") == ("cover_metric_strip", None)
    assert canonical_layout("estate_map") == ("current_state_estate_map", None)
    assert canonical_layout("option_matrix") == ("option_score_matrix", None)
    assert canonical_layout("target_operating_model") == ("platform_operating_model_hub", None)

    layout, warning = canonical_layout("totally_custom_layout")
    assert layout == "content"
    assert warning == "unknown_layout:totally_custom_layout"


def test_component_tree_for_risk_heatmap_slide():
    tree = component_tree_for_slide({
        "layout": "risk_matrix",
        "archetype": "risk_heatmap",
        "title": "Risks",
        "heatmap": [{"label": "Data migration", "likelihood": "high", "impact": "high"}],
    })

    assert tree["template"] == "risk_matrix"
    assert [component["type"] for component in tree["components"]] == [
        "TitleBlock",
        "RiskMatrix",
        "RiskRegisterTable",
        "Footer",
    ]


def test_slide_templates_cover_required_catalog_entries():
    required = {
        "title",
        "section",
        "agenda",
        "content",
        "comparison",
        "stat_cards",
        "callout",
        "executive_summary",
        "recommendation",
        "chart",
        "financial_model",
        "table",
        "timeline",
        "architecture",
        "risk_matrix",
        "risk_register",
        "operating_model",
        "investment_case",
        "appendix",
        "cover_metric_strip",
        "current_state_estate_map",
        "impact_scorecard_bars",
        "option_score_matrix",
        "platform_operating_model_hub",
        "roadmap_phase_cards",
        "risk_control_rows",
        "decision_ask_panel",
    }

    assert required.issubset(SLIDE_TEMPLATES)


def test_component_tree_for_board_pack_slide_uses_rich_payloads():
    tree = component_tree_for_slide({
        "layout": "option_score_matrix",
        "title": "Choose the managed platform path",
        "options": [
            {
                "name": "Managed platform",
                "summary": "Fastest path to governed reuse.",
                "scores": {"cost": 3, "control": 2, "adoption": 3},
                "recommended": True,
            }
        ],
    })

    assert tree["template"] == "option_score_matrix"
    comparison = next(component for component in tree["components"] if component["type"] == "ComparisonCard")
    assert comparison["columns"][0]["name"] == "Managed platform"
