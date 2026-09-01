"""The suite must not reach a real model.

A test that calls a provider asserts on whatever that provider returned on the
day, so it fails at random in CI and passes on a laptop. Two long-standing
flakes in this suite were exactly that, and finding them one at a time did not
stop the next one being written. The guard in conftest does.
"""
from __future__ import annotations

import pytest

from app.services.agent import model_client


def test_an_ordinary_test_cannot_reach_a_model():
    with pytest.raises(AssertionError, match="reached a real model"):
        model_client.complete([{"role": "user", "content": "hello"}], role="direct_answer")


def test_the_guard_names_the_two_ways_out():
    """The message has to say what to do, or the next person deletes the guard."""
    with pytest.raises(AssertionError) as caught:
        model_client.complete([{"role": "user", "content": "hello"}], role="research_planner")
    message = str(caught.value)
    assert "model_client.complete" in message
    assert "uses_model_client" in message
    assert "research_planner" in message


@pytest.mark.uses_model_client
def test_the_marker_lifts_the_guard():
    """Tests of the client itself opt out, and get the real function back."""
    assert model_client.complete.__module__ == model_client.__name__
