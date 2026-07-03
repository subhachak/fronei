from __future__ import annotations

import json
import logging

from app.services.agent import model_client
from app.services.agent.known_facts import upsert_fact

logger = logging.getLogger(__name__)


FACT_EXTRACTOR_PROMPT = """Extract structured facts from the research synthesis.

Return only a JSON array of up to 10 objects:
[{"entity_id":"...","entity_type":"...","fact_key":"...","fact_value":"..."}]

Rules:
- Include only facts explicitly stated in the synthesis.
- Do not invent entities, keys, values, or implications.
- Use stable, short snake_case fact_key values.
- Use concise fact_value strings.
- Return [] when there are no durable facts worth storing.
"""


def extract_and_store_facts(
    user_id: str,
    conversation_id: str,
    synthesis: str,
    *,
    db,
) -> int:
    """Extract structured facts from a synthesis and upsert them.

    Best-effort by design: extraction/storage failures are logged and never
    propagated to the caller.
    """
    try:
        if not user_id.strip() or not conversation_id.strip() or not synthesis.strip():
            return 0
        response = model_client.simple_completion(
            FACT_EXTRACTOR_PROMPT,
            synthesis,
            role="fact_extractor",
            max_tokens=512,
            timeout_s=14,
        )
        try:
            parsed = json.loads(response.text)
        except Exception as exc:
            logger.warning("fact_extraction_parse_error", extra={"error": str(exc)[:500]})
            return 0
        if not isinstance(parsed, list):
            logger.warning("fact_extraction_parse_error", extra={"error": "response was not a JSON array"})
            return 0
        stored = 0
        for item in parsed[:10]:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            entity_type = str(item.get("entity_type") or "").strip()
            fact_key = str(item.get("fact_key") or "").strip()
            fact_value = str(item.get("fact_value") or "").strip()
            if not all([entity_id, entity_type, fact_key, fact_value]):
                continue
            upsert_fact(
                user_id,
                entity_id,
                entity_type,
                fact_key,
                fact_value,
                source_conversation_id=conversation_id,
                db=db,
            )
            stored += 1
        return stored
    except Exception as exc:
        logger.warning("fact_extraction_error", extra={"error": str(exc)[:500]})
        return 0
