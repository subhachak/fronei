from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.db.models import RoutingDecisionFeedback, RoutingSignalCandidate, SessionLocal

logger = logging.getLogger(__name__)

EscalationRoute = Literal["web_fast", "agentic"]


@dataclass(frozen=True)
class SignalMatch:
    signal_group: str
    phrase: str
    suggested_route: EscalationRoute
    source: str = "bootstrap"
    candidate_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "signal_group": self.signal_group,
            "phrase": self.phrase,
            "suggested_route": self.suggested_route,
            "source": self.source,
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True)
class SignalGroup:
    id: str
    suggested_route: EscalationRoute
    terms: tuple[str, ...]
    description: str


@dataclass
class RoutingSignalDecision:
    matched_signals: list[SignalMatch] = field(default_factory=list)
    suggested_route: EscalationRoute | None = None

    @property
    def matched_groups(self) -> list[str]:
        seen: list[str] = []
        for match in self.matched_signals:
            if match.signal_group not in seen:
                seen.append(match.signal_group)
        return seen


BOOTSTRAP_SIGNAL_GROUPS: tuple[SignalGroup, ...] = (
    SignalGroup(
        id="currentness",
        suggested_route="web_fast",
        terms=(
            "latest",
            "current",
            "today",
            "recent",
            "as of",
            "now",
            "this year",
            "updated",
            "announced",
            "release",
        ),
        description="The answer may depend on recent or current facts.",
    ),
    SignalGroup(
        id="volatile_product_catalog",
        suggested_route="web_fast",
        terms=(
            "model",
            "models",
            "llm",
            "api model",
            "provider",
            "pricing",
            "plans",
            "version",
            "availability",
            "openai",
            "anthropic",
            "claude",
            "gemini",
            "google",
            "gpt",
        ),
        description="Product, provider, API, and pricing catalogs change frequently.",
    ),
    SignalGroup(
        id="recommendation_selection",
        suggested_route="web_fast",
        terms=(
            "best",
            "recommend",
            "should i use",
            "what should i use",
            "compare",
            "better",
            "alternative",
            "worth",
        ),
        description="Recommendations among current options should be source-grounded.",
    ),
    SignalGroup(
        id="high_stakes",
        suggested_route="agentic",
        terms=(
            "legal",
            "regulatory",
            "medical",
            "health",
            "kidney",
            "kidneys",
            "renal",
            "creatine",
            "supplement",
            "supplements",
            "supplementation",
            "dosage",
            "dose",
            "kidney disease",
            "kidney function",
            "blood pressure",
            "side effects",
            "adverse effects",
            "safe long term",
            "long-term safety",
            "financial",
            "invest",
            "investment",
            "retirement",
            "compliance",
            "risk",
            "policy",
            "contract",
        ),
        description="High-stakes domains need fuller routing and guardrails.",
    ),
    SignalGroup(
        id="owner_reliability_research",
        suggested_route="agentic",
        terms=(
            "owner reviews",
            "owner reports",
            "real-world reliability",
            "real world reliability",
            "failure rate",
            "failure rates",
            "long-term owner",
            "long term owner",
            "after 1-2 years",
            "after 1–2 years",
            "degradation",
            "warranty claims",
        ),
        description="Owner-experience and durability questions need broader evidence than a quick web lookup.",
    ),
    SignalGroup(
        id="workplace_policy_evidence",
        suggested_route="agentic",
        terms=(
            "four-day work week",
            "four day work week",
            "4-day work week",
            "4 day work week",
            "4 day week",
            "reduced work week",
            "compressed work week",
            "productivity and retention",
            "employee retention",
            "pilot program",
        ),
        description="Evidence-backed workplace policy and retention/productivity decisions need source-grounded synthesis.",
    ),
    # Phase 13a — plainly-phrased time-sensitive factual questions ask about current, variable
    # real-world state (wait times, processing delays, backlogs) that require source-grounded
    # evidence rather than general knowledge.
    SignalGroup(
        id="time_sensitive_factual",
        suggested_route="web_fast",
        terms=(
            "how long does it take",
            "how long do they take",
            "how long will it take",
            "how long is the wait",
            "wait time",
            "wait times",
            "waiting time",
            "waiting times",
            "processing time",
            "processing times",
            "turnaround time",
            "turnaround times",
            "in practice",
            "in reality",
            "actually takes",
            "currently taking",
            "real-world experience",
            "real world experience",
            "how long does",
            "how long is",
            "backlog",
            "scheduling backlog",
        ),
        description="Plainly-phrased queries about current real-world wait/processing times need source-grounded lookup, not general knowledge.",
    ),
    # Enumeration/count queries ("how many X are there tomorrow", "list of Y on date Z")
    # need multi-source synthesis to itemize correctly — a 2-source web_fast pass tends
    # to fabricate a confident count from a general schedule/overview page.
    SignalGroup(
        id="enumeration_count_query",
        suggested_route="agentic",
        terms=(
            "how many",
            "number of",
            "list of",
            "which matches",
            "which games",
            "which events",
            "which flights",
        ),
        description="Enumeration/count/list queries need the fuller research runtime's multi-source synthesis, not a 2-source quick pass.",
    ),
)

_GROUPS_BY_ID = {group.id: group for group in BOOTSTRAP_SIGNAL_GROUPS}


# Subjects that are immutable regardless of freshness-keyword phrasing.
# "Latest value of pi" or "current speed of light" contain freshness keywords
# but the subject never changes — forcing research wastes time and cost and
# produces a worse answer than a direct response from model knowledge.
# This list doesn't need to be exhaustive: the fallback is still research,
# which is safe. It only needs to cover cases where research is clearly wrong.
# Expand as new false-positives appear in eval runs.
_TIMELESS_SUBJECT_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        # Mathematical constants & expressions
        r"\bpi\b",
        r"\beuler(?:'s)? number\b",
        r"\bgolden ratio\b",
        r"\bsqrt\b",
        r"\bsquare root\b",
        r"\bfibonacci\b",
        r"\bprime number",
        r"\bmathematical constant",
        # Physical constants
        r"\bspeed of light\b",
        r"\bplanck(?:'s)? constant\b",
        r"\bavogadro",
        r"\bgravitational constant\b",
        r"\belectron (mass|charge)\b",
        # Historical / definitional
        r"\bwhat does .{1,40} stand for\b",
        r"\bdefinition of\b",
        r"\bwhat is the meaning of\b",
        r"\bboding point of water\b",
        r"\bboiling point\b",
        r"\bfreezing point\b",
        r"\bmelting point\b",
    ]
)


def _is_timeless_subject(message: str) -> bool:
    """Return True if the message subject is immutable regardless of
    freshness-keyword phrasing — these should route direct, not research."""
    msg = message.lower()
    return any(p.search(msg) for p in _TIMELESS_SUBJECT_PATTERNS)


def evaluate_routing_signals(message: str) -> RoutingSignalDecision:
    text = _normalize(message)
    if not text:
        return RoutingSignalDecision()

    matches = _bootstrap_matches(text)
    # Remove "currentness" matches when the subject is a timeless fact so
    # freshness keywords don't force unnecessary research. Other signal groups
    # are unaffected — e.g. a timeless constant asked in a product-catalog
    # context can still match volatile_product_catalog if applicable.
    if _is_timeless_subject(message):
        matches = [m for m in matches if m.signal_group != "currentness"]
    matches.extend(_approved_candidate_matches(text))
    suggested_route = _most_restrictive_route(matches)
    return RoutingSignalDecision(matched_signals=matches, suggested_route=suggested_route)


def bootstrap_signal_groups_payload() -> list[dict]:
    return [
        {
            "id": group.id,
            "suggested_route": group.suggested_route,
            "terms": list(group.terms),
            "description": group.description,
        }
        for group in BOOTSTRAP_SIGNAL_GROUPS
    ]


def record_routing_feedback(
    *,
    turn_id: str,
    user_id: str,
    conversation_id: str | None,
    message: str,
    selected_route: str,
    final_route: str,
    matched_signals: list[dict],
    outcome: str = "completed",
) -> None:
    db = SessionLocal()
    try:
        existing = db.get(RoutingDecisionFeedback, turn_id)
        if existing:
            return
        now = datetime.now(timezone.utc)
        db.add(
            RoutingDecisionFeedback(
                turn_id=turn_id,
                user_id=user_id,
                conversation_id=conversation_id,
                message=message,
                selected_route=selected_route,
                final_route=final_route,
                matched_signals_json=json.dumps(matched_signals),
                outcome=outcome,
                created_at=now,
            )
        )
        for phrase, group_id, suggested_route in _candidate_phrases(message, selected_route, final_route, matched_signals):
            _upsert_candidate(db, phrase=phrase, signal_group=group_id, suggested_route=suggested_route, turn_id=turn_id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Fronei routing feedback write failed")
    finally:
        db.close()


def list_signal_candidates(status: str | None = None, limit: int = 100) -> dict:
    db = SessionLocal()
    try:
        query = db.query(RoutingSignalCandidate)
        if status:
            query = query.filter(RoutingSignalCandidate.status == status)
        rows = (
            query.order_by(
                RoutingSignalCandidate.status.asc(),
                RoutingSignalCandidate.confidence.desc(),
                RoutingSignalCandidate.support_count.desc(),
                RoutingSignalCandidate.updated_at.desc(),
            )
            .limit(max(1, min(500, limit)))
            .all()
        )
        return {
            "bootstrap_groups": bootstrap_signal_groups_payload(),
            "candidates": [_candidate_payload(row) for row in rows],
        }
    finally:
        db.close()


def set_signal_candidate_status(candidate_id: str, status: str) -> dict | None:
    if status not in {"candidate", "approved", "rejected", "auto_active"}:
        raise ValueError("Invalid routing signal status")
    db = SessionLocal()
    try:
        row = db.get(RoutingSignalCandidate, candidate_id)
        if not row:
            return None
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
        return _candidate_payload(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _bootstrap_matches(text: str) -> list[SignalMatch]:
    matches: list[SignalMatch] = []
    for group in BOOTSTRAP_SIGNAL_GROUPS:
        for term in group.terms:
            if _term_matches(text, term):
                matches.append(SignalMatch(signal_group=group.id, phrase=term, suggested_route=group.suggested_route))
                break
    return matches


def _approved_candidate_matches(text: str) -> list[SignalMatch]:
    db = SessionLocal()
    try:
        rows = (
            db.query(RoutingSignalCandidate)
            .filter(RoutingSignalCandidate.status.in_(("approved", "auto_active")))
            .all()
        )
        matches = []
        for row in rows:
            if row.normalized_phrase and row.normalized_phrase in text:
                matches.append(
                    SignalMatch(
                        signal_group=row.signal_group,
                        phrase=row.phrase,
                        suggested_route=row.suggested_route,  # type: ignore[arg-type]
                        source="learned",
                        candidate_id=row.id,
                    )
                )
        return matches
    except Exception as exc:
        logger.debug("Fronei learned routing signals unavailable: %s", exc)
        return []
    finally:
        db.close()


def _most_restrictive_route(matches: list[SignalMatch]) -> EscalationRoute | None:
    if any(match.suggested_route == "agentic" for match in matches):
        return "agentic"
    if any(match.suggested_route == "web_fast" for match in matches):
        return "web_fast"
    return None


def _candidate_phrases(
    message: str,
    selected_route: str,
    final_route: str,
    matched_signals: list[dict],
) -> list[tuple[str, str, EscalationRoute]]:
    text = _normalize(message)
    if not text:
        return []
    route: EscalationRoute | None = None
    if final_route in {"document", "research_document"} or selected_route == "agentic":
        route = "agentic"
    elif final_route == "research" or selected_route == "web_fast":
        route = "web_fast"
    if route is None:
        return []

    groups = [str(item.get("signal_group") or "") for item in matched_signals if isinstance(item, dict)]
    group_id = next((group for group in groups if group in _GROUPS_BY_ID), "learned_escalation")
    phrases = _extract_candidate_phrases(text)
    return [(phrase, group_id, route) for phrase in phrases]


def _extract_candidate_phrases(text: str) -> list[str]:
    stopwords = {
        "what",
        "which",
        "should",
        "would",
        "could",
        "about",
        "with",
        "from",
        "that",
        "this",
        "there",
        "their",
        "between",
        "general",
        "purpose",
        "please",
    }
    tokens = [token for token in re.findall(r"[a-z0-9][a-z0-9.+-]*", text) if token not in stopwords and len(token) > 2]
    phrases: list[str] = []
    for size in (3, 2):
        for idx in range(0, max(0, len(tokens) - size + 1)):
            phrase = " ".join(tokens[idx : idx + size])
            if phrase not in phrases:
                phrases.append(phrase)
            if len(phrases) >= 3:
                return phrases
    for token in tokens:
        if token not in phrases:
            phrases.append(token)
        if len(phrases) >= 3:
            break
    return phrases


def _upsert_candidate(db, *, phrase: str, signal_group: str, suggested_route: EscalationRoute, turn_id: str) -> None:
    normalized = _normalize(phrase)
    if not normalized:
        return
    candidate_id = _candidate_id(normalized, signal_group, suggested_route)
    row = db.get(RoutingSignalCandidate, candidate_id)
    examples: list[str] = []
    now = datetime.now(timezone.utc)
    if row:
        try:
            examples = json.loads(row.example_turn_ids_json or "[]")
        except Exception:
            examples = []
        row.support_count += 1
        if turn_id not in examples:
            examples = [*examples[-9:], turn_id]
        row.example_turn_ids_json = json.dumps(examples)
        row.confidence = _confidence(row.support_count, row.false_positive_count)
        if row.status == "candidate" and row.support_count >= 20 and row.confidence >= 0.9:
            row.status = "auto_active"
        row.updated_at = now
        return
    db.add(
        RoutingSignalCandidate(
            id=candidate_id,
            phrase=phrase,
            normalized_phrase=normalized,
            signal_group=signal_group,
            suggested_route=suggested_route,
            confidence=_confidence(1, 0),
            support_count=1,
            false_positive_count=0,
            example_turn_ids_json=json.dumps([turn_id]),
            status="candidate",
            source="learned",
            created_at=now,
            updated_at=now,
        )
    )


def _candidate_payload(row: RoutingSignalCandidate) -> dict:
    try:
        examples = json.loads(row.example_turn_ids_json or "[]")
    except Exception:
        examples = []
    return {
        "id": row.id,
        "phrase": row.phrase,
        "normalized_phrase": row.normalized_phrase,
        "signal_group": row.signal_group,
        "suggested_route": row.suggested_route,
        "confidence": row.confidence,
        "support_count": row.support_count,
        "false_positive_count": row.false_positive_count,
        "example_turn_ids": examples if isinstance(examples, list) else [],
        "status": row.status,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _confidence(support_count: int, false_positive_count: int) -> float:
    total = max(1, support_count + false_positive_count)
    return round(max(0.0, min(1.0, support_count / total)), 4)


def _candidate_id(normalized: str, signal_group: str, suggested_route: str) -> str:
    digest = hashlib.sha1(f"{signal_group}:{suggested_route}:{normalized}".encode("utf-8")).hexdigest()[:20]
    return f"sig_{digest}"


def _normalize(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def _term_matches(text: str, term: str) -> bool:
    normalized = _normalize(term)
    if not normalized:
        return False
    if " " in normalized:
        return normalized in text
    return re.search(rf"\b{re.escape(normalized)}\b", text) is not None
