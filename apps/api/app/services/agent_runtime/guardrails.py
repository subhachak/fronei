from __future__ import annotations

import copy
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from app.db.models import DocumentTemplate, SessionLocal
from app.services.agent_runtime.models import GuardrailAction
from app.services.agent_runtime.registry import RuntimeRegistry


logger = logging.getLogger(__name__)

GuardrailBoundary = Literal["input", "planning", "tool_pre", "tool_post", "output"]
TemplateOwnerLookup = Callable[[str, str], bool]


@dataclass
class GuardrailContext:
    boundary: GuardrailBoundary
    user_id: str
    tenant_id: str | None
    tool_name: str | None
    tool_input: dict | None
    tool_output: dict | None
    request_text: str | None
    plan: dict | None
    response_text: str | None
    metadata: dict = field(default_factory=dict)


@dataclass
class GuardrailDecision:
    policy_id: str
    action: GuardrailAction
    triggered_checks: list[str]
    reason: str
    modified_payload: dict | None = None


@dataclass
class _CheckResult:
    action: GuardrailAction
    triggered: bool = False
    reason: str = ""
    modified_payload: dict | None = None


class GuardrailService:
    def __init__(
        self,
        registry: RuntimeRegistry,
        *,
        template_owner_lookup: TemplateOwnerLookup | None = None,
    ) -> None:
        self.registry = registry
        self.template_owner_lookup = template_owner_lookup or _template_belongs_to_user_db

    def evaluate(
        self,
        policy_id: str,
        context: GuardrailContext,
    ) -> GuardrailDecision:
        policy = self.registry.guardrail(policy_id)
        if not policy.enabled:
            return GuardrailDecision(
                policy_id=policy_id,
                action="allow",
                triggered_checks=[],
                reason="Policy disabled.",
            )

        triggered_checks: list[str] = []
        reasons: list[str] = []
        modified_payload: dict | None = None
        final_action: GuardrailAction = "allow"

        for check in policy.checks:
            check_type = str(check.get("type") or "")
            result = self._evaluate_check(check_type, context)
            if result.modified_payload is not None:
                modified_payload = result.modified_payload
            if result.triggered:
                triggered_checks.append(check_type)
            if result.reason:
                reasons.append(result.reason)
            final_action = _more_restrictive_action(final_action, result.action)

        if not reasons:
            reasons.append("No guardrail checks triggered.")

        return GuardrailDecision(
            policy_id=policy_id,
            action=final_action,
            triggered_checks=triggered_checks,
            reason="; ".join(reasons),
            modified_payload=modified_payload,
        )

    def evaluate_boundary(
        self,
        boundary: str,
        context: GuardrailContext,
    ) -> list[GuardrailDecision]:
        """Evaluate all enabled policies whose applies_to includes the boundary."""

        return [
            self.evaluate(policy.id, context)
            for policy in self.registry.guardrails.values()
            if policy.enabled and boundary in policy.applies_to
        ]

    def _evaluate_check(self, check_type: str, context: GuardrailContext) -> _CheckResult:
        if check_type in {"url_public_network_only", "block_private_ip_ranges"}:
            return self._check_public_url(context, check_type)
        if check_type == "strip_tool_instructions":
            return self._check_strip_tool_instructions(context)
        if check_type == "require_source_manifest":
            return self._check_require_source_manifest(context)
        if check_type == "template_belongs_to_user":
            return self._check_template_belongs_to_user(context)
        if check_type == "no_silent_default_template_fallback":
            return self._check_no_silent_default_template_fallback(context)

        logger.warning("Unknown guardrail check type: %s", check_type)
        return _CheckResult(action="allow", reason=f"Unknown check type {check_type}; allowing.")

    def _check_public_url(self, context: GuardrailContext, check_type: str) -> _CheckResult:
        url = _extract_url(context)
        if not url:
            return _CheckResult(action="allow", reason="No URL supplied.")

        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return _CheckResult(action="block", triggered=True, reason="URL scheme is not http or https.")
        if not hostname:
            return _CheckResult(action="block", triggered=True, reason="URL is missing a hostname.")
        if _hostname_looks_private(hostname):
            return _CheckResult(action="block", triggered=True, reason="URL hostname is private or local.")

        try:
            addresses = _resolve_host(hostname)
        except OSError as exc:
            if check_type == "block_private_ip_ranges":
                return _CheckResult(action="allow", reason=f"DNS resolution failed; no private IP confirmed: {exc}")
            return _CheckResult(action="block", triggered=True, reason=f"DNS resolution failed: {exc}")

        for address in addresses:
            if _is_private_address(address):
                return _CheckResult(action="block", triggered=True, reason="URL resolves to private IP range.")

        return _CheckResult(action="allow", reason="URL resolves to public network address.")

    def _check_strip_tool_instructions(self, context: GuardrailContext) -> _CheckResult:
        payload = context.tool_output
        if payload is None and context.response_text is not None:
            payload = {"response_text": context.response_text}
        if payload is None:
            return _CheckResult(action="allow", reason="No tool output to sanitize.")

        changed, sanitized = _sanitize_payload(payload)
        if not changed:
            return _CheckResult(action="allow", reason="No injected tool/system instructions found.")
        return _CheckResult(
            action="transform",
            triggered=True,
            reason="Stripped injected tool/system instruction patterns.",
            modified_payload=sanitized,
        )

    def _check_require_source_manifest(self, context: GuardrailContext) -> _CheckResult:
        if context.tool_output is None:
            return _CheckResult(action="allow", reason="No tool output to inspect for source manifest.")
        payload = context.tool_output or {}
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if isinstance(sources, list) and len(sources) > 0:
            return _CheckResult(action="allow", reason="Source manifest present.")
        return _CheckResult(action="block", triggered=True, reason="Tool output is missing a non-empty sources list.")

    def _check_template_belongs_to_user(self, context: GuardrailContext) -> _CheckResult:
        template_id = _template_id_from_input(context.tool_input)
        if not template_id or template_id == "default":
            return _CheckResult(action="allow", reason="No user template selected.")
        if self.template_owner_lookup(template_id, context.user_id):
            return _CheckResult(action="allow", reason="Template belongs to user.")
        return _CheckResult(action="block", triggered=True, reason="Selected template does not belong to user.")

    def _check_no_silent_default_template_fallback(self, context: GuardrailContext) -> _CheckResult:
        template_id = _template_id_from_input(context.tool_input)
        requested_template = _requested_template_from_plan(context.plan)
        if template_id == "default" and requested_template and requested_template != "default":
            return _CheckResult(
                action="block",
                triggered=True,
                reason="Default template fallback attempted despite a user-selected template.",
            )
        return _CheckResult(action="allow", reason="No silent default template fallback detected.")


def _extract_url(context: GuardrailContext) -> str | None:
    tool_input = context.tool_input or {}
    url = tool_input.get("url")
    if isinstance(url, str):
        return url
    query = tool_input.get("query")
    if isinstance(query, str) and query.startswith(("http://", "https://")):
        return query
    return None


def _resolve_host(hostname: str) -> set[str]:
    if _is_ip_literal(hostname):
        return {hostname}
    infos = socket.getaddrinfo(hostname, None)
    return {info[4][0] for info in infos if info and info[4]}


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def _hostname_looks_private(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered in {"localhost", "metadata.google.internal"}:
        return True
    if lowered.endswith(".local") or lowered.endswith(".internal"):
        return True
    if _is_ip_literal(lowered):
        return _is_private_address(lowered)
    return False


def _is_private_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any([
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,
    ]) or str(ip) == "169.254.169.254"


_INSTRUCTION_PATTERNS = [
    re.compile(r"<\s*/?\s*tool\s*>", re.IGNORECASE),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
    re.compile(r"(?im)^\s*SYSTEM\s*:\s*.*$"),
    re.compile(r"(?im)^\s*TOOL\s*:\s*.*$"),
    re.compile(r"(?im)^\s*DEVELOPER\s*:\s*.*$"),
]


def _sanitize_payload(payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    sanitized = copy.deepcopy(payload)
    changed = _sanitize_value_in_place(sanitized)
    return changed, sanitized


def _sanitize_value_in_place(value: Any) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if isinstance(child, str):
                new_child = _sanitize_text(child)
                if new_child != child:
                    value[key] = new_child
                    changed = True
            else:
                changed = _sanitize_value_in_place(child) or changed
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            if isinstance(child, str):
                new_child = _sanitize_text(child)
                if new_child != child:
                    value[idx] = new_child
                    changed = True
            else:
                changed = _sanitize_value_in_place(child) or changed
    return changed


def _sanitize_text(text: str) -> str:
    sanitized = text
    for pattern in _INSTRUCTION_PATTERNS:
        sanitized = pattern.sub("", sanitized)
    return sanitized.strip()


def _template_id_from_input(tool_input: dict | None) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("template_id")
    if isinstance(value, str):
        return value
    document_brief = tool_input.get("document_brief")
    if isinstance(document_brief, dict) and isinstance(document_brief.get("template_id"), str):
        return document_brief["template_id"]
    return None


def _requested_template_from_plan(plan: dict | None) -> str | None:
    if not isinstance(plan, dict):
        return None
    for key in ("template_id", "selected_template_id", "user_template_id"):
        value = plan.get(key)
        if isinstance(value, str):
            return value
    output_contract = plan.get("output_contract")
    if isinstance(output_contract, dict):
        value = output_contract.get("template_id") or output_contract.get("selected_template_id")
        if isinstance(value, str):
            return value
    document_brief = plan.get("document_brief")
    if isinstance(document_brief, dict):
        value = document_brief.get("template_id") or document_brief.get("selected_template_id")
        if isinstance(value, str):
            return value
    return None


def _more_restrictive_action(current: GuardrailAction, candidate: GuardrailAction) -> GuardrailAction:
    order = {
        "allow": 0,
        "allow_with_constraints": 1,
        "transform": 2,
        "redact": 2,
        "require_research": 3,
        "require_judge": 3,
        "ask_user": 4,
        "stop_with_caveat": 5,
        "escalate_to_admin": 6,
        "block": 7,
    }
    return candidate if order[candidate] > order[current] else current


def max_boundary_action(decisions: list[GuardrailDecision]) -> GuardrailAction:
    """Return the effective action for a boundary's guardrail decisions."""

    result: GuardrailAction = "allow"
    for decision in decisions:
        result = _more_restrictive_action(result, decision.action)
    return result


def _template_belongs_to_user_db(template_id: str, user_id: str) -> bool:
    # TODO Phase E: replace standalone SessionLocal usage with request/job-scoped DI session.
    db = SessionLocal()
    try:
        return db.query(DocumentTemplate).filter(
            DocumentTemplate.public_id == template_id,
            DocumentTemplate.user_id == user_id,
            DocumentTemplate.is_active.is_(True),
        ).first() is not None
    finally:
        db.close()
