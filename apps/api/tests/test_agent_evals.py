from app.evals.runner import assert_evals_pass, load_scenarios


def test_golden_agent_scenarios_are_substantial_and_pass():
    scenarios = load_scenarios()
    assert len(scenarios) >= 15
    assert len({scenario.category for scenario in scenarios}) >= 6
    assert_evals_pass()
