from unittest.mock import MagicMock

from app.services.refinement import should_refine


def _profile(has_prompt=True):
    p = MagicMock()
    p.rewrite_prompt = "Rewrite this." if has_prompt else None
    return p


def test_should_refine_skips_raw_mode():
    assert should_refine("x " * 60, "raw", _profile()) is False


def test_should_refine_skips_no_profile():
    assert should_refine("x " * 60, "default", None) is False


def test_should_refine_skips_no_rewrite_prompt():
    assert should_refine("x " * 60, "default", _profile(has_prompt=False)) is False


def test_should_refine_skips_short_response():
    assert should_refine("too short", "default", _profile()) is False


def test_should_refine_approves_long_response_with_profile():
    assert should_refine("word " * 125, "client_ready", _profile()) is True
