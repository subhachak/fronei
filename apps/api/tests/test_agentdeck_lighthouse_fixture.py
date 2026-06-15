import json
from pathlib import Path


def test_lighthouse_smoke_fixture_is_well_formed():
    path = Path(__file__).parent / "golden" / "lighthouse" / "enterprise_ai_platform_consolidation_smoke.json"
    data = json.loads(path.read_text())
    assert data["expected_doc_type"] == "presentation"
    assert data["expected_theme"] in {"dark", "light"}
    assert "steering committee" in data["prompt"].lower()
    assert data["acceptance_notes"]
