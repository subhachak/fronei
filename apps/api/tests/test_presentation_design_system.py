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
    }

    assert required.issubset(SLIDE_TEMPLATES)
