from unittest.mock import patch
from app.services.router import choose_route, load_policy


def test_forced_model():
    route = choose_route("write code", force_model="gpt-4.1-mini")
    assert route.primary_model == "gpt-4.1-mini"
    assert "forced" in route.reason.lower()


def test_architecture_route_balanced():
    route = choose_route(
        "Create a production enterprise architecture for a model router",
        profile="balanced",
    )
    assert route.task_type == "architecture"
    assert route.primary_model


def test_deep_research_forces_research_high():
    route = choose_route("tell me about market trends", deep_research=True)
    assert route.task_type == "research"
    assert route.complexity == "high"


def test_web_search_prefers_native_search_model():
    route = choose_route("latest news on AI", profile="balanced", web_search=True)
    assert any(
        x in route.primary_model
        for x in ["perplexity", "gemini", "sonar"]
    )


def test_fallback_chain_includes_safety_net():
    route = choose_route("hello", profile="balanced")
    safety_net = {"claude-sonnet-4-6", "gpt-4.1-mini", "gemini/gemini-2.5-flash"}
    assert safety_net & set(route.fallbacks)


def test_cost_saver_profile():
    route = choose_route("summarize this document", profile="cost_saver")
    assert route.profile == "cost_saver"


def test_policy_loads():
    policy = load_policy()
    assert "routes" in policy
    assert "default" in policy
