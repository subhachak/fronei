from app.services.fingerprint_extractor import _build_rewrite_prompt, _parse_fingerprint


def test_parse_fingerprint_valid_json():
    raw = """
    {
      "sentence_length": "medium",
      "formality": "professional",
      "directness": "high",
      "hedging": "low",
      "structure": "mixed",
      "technical_depth": "high",
      "preferred_phrases": [],
      "forbidden_phrases": [],
      "avoid_patterns": [],
      "signature_patterns": [],
      "tone_by_audience": {}
    }
    """
    result = _parse_fingerprint(raw)
    assert result is not None
    assert result["sentence_length"] == "medium"


def test_parse_fingerprint_strips_markdown_fences():
    raw = '```json\n{"sentence_length": "short"}\n```'
    result = _parse_fingerprint(raw)
    assert result is not None


def test_parse_fingerprint_returns_none_on_garbage():
    assert _parse_fingerprint("not json at all") is None


def test_build_rewrite_prompt_contains_key_sections():
    fp = {
        "sentence_length": "short",
        "formality": "executive",
        "directness": "high",
        "hedging": "low",
        "structure": "prose_heavy",
        "technical_depth": "expert",
        "signature_patterns": ["leads with rec"],
        "forbidden_phrases": ["leverage"],
        "avoid_patterns": ["passive endings"],
    }
    prompt = _build_rewrite_prompt(fp)
    assert "executive" in prompt
    assert "leads with rec" in prompt
    assert "leverage" in prompt
    assert "passive endings" in prompt
