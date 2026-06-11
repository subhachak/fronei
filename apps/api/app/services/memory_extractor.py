"""
Background service that extracts persistent facts from each conversation turn
and stores them in user_memories. Runs in the thread pool from memory_writer.
Silent failure — no memory is ever lost on error.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import re

from litellm import completion

from app.db.models import SessionLocal, UserMemory

_pool = ThreadPoolExecutor(max_workers=2)
_MODEL = "gemini/gemini-2.5-flash"
_MAX_CHARS = 600

_PROMPT = """\
Extract persistent facts about the USER from this conversation turn.
Only extract facts useful in FUTURE conversations: domain expertise, tools they use,
ongoing projects, stated preferences, constraints, personal context.
Do NOT extract facts about the topic being discussed.
Return a JSON array of objects: [{"content": "...", "category": "..."}]
category must be one of: work, preference, project, tool, constraint, personal
Return [] if nothing is worth remembering. Output only valid JSON — no fences, no explanation."""


def _extract(user_id: str, conv_id: int, user_msg: str, assistant_answer: str) -> None:
    try:
        u = user_msg[:_MAX_CHARS]
        a = assistant_answer[:_MAX_CHARS]
        resp = completion(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": f"User message: {u}\nAssistant answer: {a}"},
            ],
            temperature=0.1,
            max_tokens=256,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        facts = json.loads(raw)
        if not isinstance(facts, list):
            return
        db = SessionLocal()
        try:
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                content  = str(fact.get("content", "")).strip()
                category = str(fact.get("category", "general")).strip()
                if not content:
                    continue
                db.add(UserMemory(
                    user_id=user_id,
                    content=content,
                    category=category,
                    source_conversation_id=conv_id,
                ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # silent failure


def schedule(user_id: str, conv_id: int, user_msg: str, assistant_answer: str) -> None:
    """Fire-and-forget: extract and store memorable facts from this turn."""
    _pool.submit(_extract, user_id, conv_id, user_msg, assistant_answer)
