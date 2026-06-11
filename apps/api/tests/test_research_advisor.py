from app.services.planner import passthrough
from app.services.research_advisor import advise_research


def test_rules_recommend_research_for_vendor_current_decision():
    plan = passthrough("Compare Snowflake Cortex and Amazon Bedrock pricing for enterprise RAG")
    rec = advise_research("Compare Snowflake Cortex and Amazon Bedrock pricing for enterprise RAG", plan)
    assert rec.recommend is True
    assert rec.confidence in {"medium", "high"}
    assert "vendor_context" in rec.risk_factors


def test_planner_can_recommend_when_rules_do_not():
    plan = passthrough("Should we standardize on one platform?")
    plan.recommend_deep_research = True
    plan.research_reason = "This depends on external platform maturity and market evidence."
    plan.research_risk_factors = ["market_context"]
    plan.research_confidence = "medium"
    rec = advise_research("Should we standardize on one platform?", plan)
    assert rec.recommend is True
    assert rec.source == "planner"
    assert rec.reason == plan.research_reason


def test_attached_document_without_external_signal_does_not_interrupt():
    plan = passthrough("Summarize the uploaded architecture document")
    rec = advise_research("Summarize the uploaded architecture document", plan, has_attached_documents=True)
    assert rec.recommend is False


def test_rules_recommend_research_for_appliance_purchase():
    plan = passthrough("Find one suitable for me from the dishwasher options")
    rec = advise_research("Find one suitable for me from the dishwasher options", plan)
    assert rec.recommend is True
    assert "purchase_decision" in rec.risk_factors
    assert "consumer_product" in rec.risk_factors
