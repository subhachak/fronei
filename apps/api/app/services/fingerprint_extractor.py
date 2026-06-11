"""
Extracts a style fingerprint from a user's writing samples.
Runs as a background task after sample submission.

The fingerprint captures: sentence style, formality, directness, hedging level,
structure preference, technical depth, preferred/forbidden phrases, and tone by audience.
It also generates a rewrite_prompt — a ready-to-use system prompt the refinement
service inserts as the LLM's instructions when rewriting in the user's voice.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from litellm import completion

from app.db.models import SessionLocal, TwinProfile, WritingSample

_pool = ThreadPoolExecutor(max_workers=2)
_MODEL = "claude-haiku-4-5-20251001"
_MAX_SAMPLE_CHARS = 1200


_EXTRACTION_PROMPT = """\
You are a professional writing analyst. Read the writing samples below and extract
a precise style fingerprint for this author.

Return ONLY valid JSON matching this exact schema — no markdown, no explanation:

{
  "sentence_length":    "short|medium|long",
  "formality":          "casual|professional|formal|executive",
  "directness":         "low|medium|high",
  "hedging":            "low|medium|high",
  "structure":          "prose_heavy|bullet_heavy|mixed",
  "technical_depth":    "low|medium|high|expert",
  "preferred_phrases":  ["up to 6 actual phrases this author uses"],
  "forbidden_phrases":  ["up to 8 generic AI phrases this author clearly avoids"],
  "avoid_patterns":     ["up to 5 structural or rhetorical patterns to suppress"],
  "signature_patterns": ["up to 4 distinctive things this author does well"],
  "tone_by_audience": {
    "client":     "one sentence describing their client-facing tone",
    "executive":  "one sentence describing their exec-facing tone",
    "technical":  "one sentence describing their technical tone",
    "internal":   "one sentence describing their internal/team tone"
  }
}

forbidden_phrases should always include these generic AI phrases if not already
in the author's writing:
["In today's fast-paced world", "leverage", "synergies", "game-changer",
 "robust", "seamless", "cutting-edge", "best-in-class", "holistic",
 "actionable insights", "move the needle", "it is worth noting that",
 "it is important to note", "in conclusion", "to summarize"]

Be specific and accurate. Base every field on evidence from the samples.\
"""


_REWRITE_PROMPT_TEMPLATE = """\
You are a writing editor. Rewrite the text below to match this author's exact voice.

STYLE FINGERPRINT:
- Sentence length: {sentence_length}
- Formality: {formality}
- Directness: {directness} (low hedging: {hedging})
- Structure preference: {structure}
- Technical depth: {technical_depth}

SIGNATURE PATTERNS (preserve these):
{signature_patterns}

PHRASES TO AVOID (the author never writes like this):
{forbidden_phrases}

PATTERNS TO SUPPRESS:
{avoid_patterns}

RULES:
1. Match the author's voice exactly — not generic AI voice.
2. Remove all AI slop: no filler openings, no obvious transitions, no empty conclusions.
3. Preserve all factual content, recommendations, and technical substance.
4. Do not add caveats or disclaimers the original lacked.
5. Do not pad the response. If it can be shorter without losing meaning, make it shorter.
6. Output ONLY the rewritten text — no preamble, no explanation.\
"""


def _build_rewrite_prompt(fingerprint: dict) -> str:
    return _REWRITE_PROMPT_TEMPLATE.format(
        sentence_length=fingerprint.get("sentence_length", "medium"),
        formality=fingerprint.get("formality", "professional"),
        directness=fingerprint.get("directness", "high"),
        hedging=fingerprint.get("hedging", "low"),
        structure=fingerprint.get("structure", "mixed"),
        technical_depth=fingerprint.get("technical_depth", "high"),
        signature_patterns="\n".join(
            f"- {p}" for p in fingerprint.get("signature_patterns", [])
        ) or "- (none identified yet)",
        forbidden_phrases="\n".join(
            f"- {p}" for p in fingerprint.get("forbidden_phrases", [])
        ) or "- (none identified yet)",
        avoid_patterns="\n".join(
            f"- {p}" for p in fingerprint.get("avoid_patterns", [])
        ) or "- (none identified yet)",
    )


def _parse_fingerprint(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def extract_and_store(user_id: str) -> None:
    """
    Fetch all writing samples for user_id, extract fingerprint via LLM,
    build rewrite_prompt, and persist to twin_profiles.

    Called as a background task — all errors are caught silently.
    """
    try:
        db = SessionLocal()
        try:
            samples = (
                db.query(WritingSample)
                .filter(WritingSample.user_id == user_id)
                .order_by(WritingSample.created_at.desc())
                .all()
            )
            if not samples:
                profile = db.query(TwinProfile).filter(TwinProfile.user_id == user_id).first()
                if profile:
                    profile.fingerprint_json = None
                    profile.rewrite_prompt = None
                    profile.extracted_at = None
                    profile.updated_at = datetime.now(timezone.utc)
                    db.commit()
                return

            parts: list[str] = []
            for i, s in enumerate(samples, 1):
                excerpt = s.content[:_MAX_SAMPLE_CHARS]
                label = f" ({s.label})" if s.label else ""
                parts.append(f"--- Sample {i}{label} ---\n{excerpt}")
            sample_block = "\n\n".join(parts)

            resp = completion(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": _EXTRACTION_PROMPT},
                    {"role": "user", "content": sample_block},
                ],
                temperature=0.1,
                max_tokens=1024,
            )
            raw = (resp.choices[0].message.content or "").strip()
            fingerprint = _parse_fingerprint(raw)
            if not fingerprint:
                return

            rewrite_prompt = _build_rewrite_prompt(fingerprint)

            profile = db.query(TwinProfile).filter(TwinProfile.user_id == user_id).first()
            if not profile:
                profile = TwinProfile(
                    user_id=user_id,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.add(profile)
            profile.fingerprint_json = json.dumps(fingerprint)
            profile.rewrite_prompt = rewrite_prompt
            profile.extracted_at = datetime.now(timezone.utc)
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def schedule(user_id: str) -> None:
    """Fire-and-forget extraction."""
    _pool.submit(extract_and_store, user_id)
