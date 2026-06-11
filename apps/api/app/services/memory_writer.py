"""
Background memory writer — replaces the rules-based running_summary entry with
an LLM-generated one-sentence summary after each response is committed.

Runs in a daemon thread so it never blocks the response. On any error
(LLM failure, DB contention, rate limit) the thread exits silently and the
rules-based fallback entry from _update_conversation_state stays in place.
"""
from concurrent.futures import ThreadPoolExecutor

from litellm import completion

_pool = ThreadPoolExecutor(max_workers=4)

from app.db.models import Conversation, SessionLocal

_MODEL = "gemini/gemini-2.5-flash"
_MAX_ANSWER_CHARS = 600
_PROMPT = (
    "Write ONE sentence summarising this conversation turn.\n"
    "Format: 'User [verb] [topic]; assistant [verb] [outcome].'\n"
    "Be specific and factual. Output ONLY the sentence — no quotes, no prefixes."
)


def _write(conv_id: int, turn_type: str, intent: str, answer: str, rules_entry: str) -> None:
    try:
        excerpt = answer[:_MAX_ANSWER_CHARS] + ("…" if len(answer) > _MAX_ANSWER_CHARS else "")
        resp = completion(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": f"Intent: {intent}\nAnswer excerpt: {excerpt}"},
            ],
            temperature=0.1,
            max_tokens=96,
        )
        sentence = (resp.choices[0].message.content or "").strip().strip('"')
        if not sentence:
            return

        new_entry = f"[{turn_type}] {sentence}"

        db = SessionLocal()
        try:
            conv = db.get(Conversation, conv_id)
            if not conv or not conv.running_summary:
                return
            lines = conv.running_summary.splitlines()
            # Only replace if the rules-based entry is still the last line.
            # This guards against a second request arriving before this thread
            # completes, which would make lines[-1] a newer entry.
            if lines and lines[-1] == rules_entry:
                lines[-1] = new_entry
                conv.running_summary = "\n".join(lines)
                db.commit()
        finally:
            db.close()
    except Exception:
        pass  # silent failure — rules-based summary stays intact


def schedule(conv_id: int, turn_type: str, intent: str, answer: str, rules_entry: str) -> None:
    """Fire-and-forget: improve the latest running_summary entry via a background LLM call."""
    _pool.submit(_write, conv_id, turn_type, intent, answer, rules_entry)
