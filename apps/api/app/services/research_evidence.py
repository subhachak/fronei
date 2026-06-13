"""Deterministic evidence metadata for research sources.

This module is the control-plane half of Deep Research v2.  It deliberately
keeps source tier/date/family decisions cheap and inspectable; claim-level LLM
reasoning can refine these priors later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse


SOURCE_TIER_OFFICIAL = "tier_1_official"
SOURCE_TIER_EXPERT = "tier_2_expert"
SOURCE_TIER_ANECDOTAL = "tier_3_anecdotal"
SOURCE_TIER_LOW = "tier_4_low_quality"

ROLE_OFFICIAL_POLICY = "official_policy"
ROLE_OPERATIONAL_REALITY = "operational_reality"
ROLE_EXPERT_INTERPRETATION = "expert_interpretation"
ROLE_ANECDOTAL_CASE = "anecdotal_case"
ROLE_STATISTICAL_DATA = "statistical_data"
ROLE_BACKGROUND = "background_context"


@dataclass(frozen=True)
class SourceEvidenceMetadata:
    source_tier: str
    source_family: str
    source_role_prior: str
    published_at: datetime | None
    updated_at: datetime | None
    source_date_confidence: str
    admission_status: str
    admission_reason: str


def host_for_url(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def source_family_for_url(url: str) -> str:
    """Return a cheap registrable-domain-ish family for diversity checks.

    This is intentionally dependency-free.  It is not a full public-suffix
    parser, but it prevents obvious overcounting such as www/docs subdomains.
    """
    host = host_for_url(url)
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if parts[-2] in {"co", "com", "org", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_source_tier(url: str, title: str = "", content: str = "", source_type: str | None = None) -> str:
    host = host_for_url(url)
    text = f"{title} {content[:1600]}".lower()
    source_type = source_type or ""
    if host.endswith(".gov") or host.endswith(".mil") or "sec.gov" in host or "clinicaltrials.gov" in host:
        return SOURCE_TIER_OFFICIAL
    if source_type in {"documentation", "pricing", "release_notes", "repository"}:
        return SOURCE_TIER_OFFICIAL
    if host.endswith(".edu") or source_type in {"academic", "pdf"}:
        return SOURCE_TIER_EXPERT
    if any(x in host for x in ["reddit.", "quora.", "lawfully.", "immihelp.", "trackitt.", "blind."]):
        return SOURCE_TIER_ANECDOTAL
    if any(x in host for x in ["medium.", "substack.", "wordpress.", "blogspot."]):
        return SOURCE_TIER_LOW
    if any(p in text for p in ["sponsored", "affiliate", "coupon", "top 10", "best tools"]):
        return SOURCE_TIER_LOW
    if any(x in host for x in ["law.", "legal", "murthy.", "aila.", "shrm.", "mayo", "nih.", "fda."]):
        return SOURCE_TIER_EXPERT
    if source_type in {"news", "commentary"}:
        return SOURCE_TIER_EXPERT
    return SOURCE_TIER_EXPERT


def classify_source_role_prior(url: str, title: str = "", content: str = "", source_type: str | None = None) -> str:
    host = host_for_url(url)
    text = f"{title} {content[:2200]}".lower()
    source_type = source_type or ""
    operational_markers = {
        "processing time", "timeline", "approval time", "approved in", "backlog",
        "delay", "wait time", "case tracker", "receipt date", "service center",
        "current reports", "community", "forum",
    }
    policy_markers = {
        "policy", "eligibility", "form i-", "regulation", "rule", "premium processing",
        "official", "guidance", "instructions", "manual", "uscis",
    }
    statistical_markers = {"data", "statistics", "dashboard", "filing", "10-k", "table", "dataset"}

    if any(x in host for x in ["reddit.", "quora.", "lawfully.", "immihelp.", "trackitt.", "blind."]):
        return ROLE_ANECDOTAL_CASE
    if any(marker in text for marker in operational_markers):
        return ROLE_OPERATIONAL_REALITY
    if source_type in {"pricing", "release_notes", "documentation", "government"} or any(marker in text for marker in policy_markers):
        return ROLE_OFFICIAL_POLICY
    if any(marker in text for marker in statistical_markers):
        return ROLE_STATISTICAL_DATA
    if source_type in {"news", "commentary", "academic", "pdf"}:
        return ROLE_EXPERT_INTERPRETATION
    return ROLE_BACKGROUND


def _coerce_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _date_from_match(match: re.Match[str]) -> datetime | None:
    raw = match.group("date") if "date" in match.groupdict() else match.group(1)
    return _coerce_datetime(raw)


def extract_source_dates(content: str, title: str = "") -> tuple[datetime | None, datetime | None, str]:
    haystack = f"{title}\n{content[:8000]}"
    meta_patterns = [
        r"(?:article:published_time|datePublished|published_time|pubdate)[\"'\s:=]+(?P<date>\d{4}-\d{2}-\d{2}(?:[T ][0-9:.\-+Z]+)?)",
        r"(?:article:modified_time|dateModified|updated_time|lastmod)[\"'\s:=]+(?P<date>\d{4}-\d{2}-\d{2}(?:[T ][0-9:.\-+Z]+)?)",
    ]
    published: datetime | None = None
    updated: datetime | None = None
    for pattern in meta_patterns:
        for match in re.finditer(pattern, haystack, re.IGNORECASE):
            dt = _date_from_match(match)
            if not dt:
                continue
            if "published" in match.group(0).lower() or "pubdate" in match.group(0).lower():
                published = published or dt
            else:
                updated = updated or dt

    visible_patterns = [
        r"(?:last updated|updated|modified|effective date)[:\s]+(?P<date>[A-Z][a-z]{2,9}\s+\d{1,2},\s+20[1-3][0-9])",
        r"(?:published|posted|date)[:\s]+(?P<date>[A-Z][a-z]{2,9}\s+\d{1,2},\s+20[1-3][0-9])",
        r"(?:last updated|updated|modified|effective date)[:\s]+(?P<date>20[1-3][0-9]-\d{1,2}-\d{1,2})",
        r"(?:published|posted|date)[:\s]+(?P<date>20[1-3][0-9]-\d{1,2}-\d{1,2})",
    ]
    for pattern in visible_patterns:
        for match in re.finditer(pattern, haystack, re.IGNORECASE):
            dt = _date_from_match(match)
            if not dt:
                continue
            marker = match.group(0).lower()
            if any(x in marker for x in ["updated", "modified", "effective"]):
                updated = updated or dt
            else:
                published = published or dt

    if published or updated:
        return published, updated, "exact"

    years = [int(y) for y in re.findall(r"\b20[1-3][0-9]\b", haystack)]
    if years:
        year_dt = datetime(max(years), 1, 1, tzinfo=timezone.utc)
        return None, year_dt, "year"
    return None, None, "unknown"


def admission_for_source(
    *,
    source_tier: str,
    source_role_prior: str,
    credibility: float,
    relevance: float,
    freshness: float,
) -> tuple[str, str]:
    if relevance < 0.18:
        return "rejected", "low relevance to the research question"
    if source_tier == SOURCE_TIER_LOW and credibility < 0.35:
        return "downgraded", "low-authority source; use only for background or contradiction checks"
    if source_role_prior == ROLE_ANECDOTAL_CASE:
        return "admitted", "anecdotal source admitted; use for operational reality only"
    if freshness < 0.25 and source_tier != SOURCE_TIER_OFFICIAL:
        return "downgraded", "dated source; avoid current claims unless corroborated"
    return "admitted", "passed deterministic evidence metadata checks"


def build_source_evidence_metadata(
    *,
    url: str,
    title: str,
    content: str,
    source_type: str | None,
    credibility: float,
    relevance: float,
    freshness: float,
) -> SourceEvidenceMetadata:
    published_at, updated_at, date_confidence = extract_source_dates(content, title)
    tier = classify_source_tier(url, title, content, source_type)
    role = classify_source_role_prior(url, title, content, source_type)
    status, reason = admission_for_source(
        source_tier=tier,
        source_role_prior=role,
        credibility=credibility,
        relevance=relevance,
        freshness=freshness,
    )
    return SourceEvidenceMetadata(
        source_tier=tier,
        source_family=source_family_for_url(url),
        source_role_prior=role,
        published_at=published_at,
        updated_at=updated_at,
        source_date_confidence=date_confidence,
        admission_status=status,
        admission_reason=reason,
    )
