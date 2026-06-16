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

# Fronei-owned builtin template IDs. Keep in sync with BUILTIN_PPTX_TEMPLATES
# in services/document_templates.py. Do not import it here to avoid cycles.
BUILTIN_SAFE_TEMPLATE_IDS: frozenset[str] = frozenset({
    "fronei-default",
    "warm-editorial",
    "modern-tech",
    "executive-navy",
    "data-product-os",
    "clean-light",
})

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
    user_role: str = "user"
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
        """Evaluate enabled policies for a boundary, honoring tool selectors.

        Guardrail policies can include both boundary selectors (``tool_pre``,
        ``tool_post``, ``output``) and tool selectors (``tool:web_search``).
        Tool selectors narrow tool boundary checks to a specific tool while
        leaving non-tool boundaries, such as final output checks, unchanged.
        """

        return [
            self.evaluate(policy.id, context)
            for policy in self.registry.guardrails.values()
            if policy.enabled and _policy_applies_to_context(policy.applies_to, boundary, context)
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
        if check_type == "source_content_public":
            return self._check_source_content_public(context)
        if check_type == "role_required":
            return self._check_role_required(context)

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
        if context.tool_name == "read_url" and isinstance(payload, dict):
            if payload.get("url") and payload.get("content"):
                return _CheckResult(action="allow", reason="Single URL source content present.")
        return _CheckResult(action="block", triggered=True, reason="Tool output is missing a non-empty sources list.")

    def _check_template_belongs_to_user(self, context: GuardrailContext) -> _CheckResult:
        template_id = _template_id_from_input(context.tool_input)
        if not template_id or template_id == "default":
            return _CheckResult(action="allow", reason="No user template selected.")
        if template_id in BUILTIN_SAFE_TEMPLATE_IDS:
            return _CheckResult(action="allow", reason="Built-in template; ownership not required.")

        user_id = context.user_id or ""
        if not user_id:
            return _CheckResult(
                action="block",
                triggered=True,
                reason="Cannot verify template ownership: user_id is missing from context.",
            )

        if self.template_owner_lookup(template_id, user_id):
            return _CheckResult(action="allow", reason="Template belongs to user.")
        return _CheckResult(
            action="block",
            triggered=True,
            reason=f"Template {template_id!r} does not belong to user {user_id!r} or does not exist.",
        )

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

    def _check_source_content_public(self, context: GuardrailContext) -> _CheckResult:
        from app.services.agent_runtime.source_classifier import classify_source_content

        output = context.tool_output or {}
        if not isinstance(output, dict):
            return _CheckResult(action="allow", reason="No structured source output to classify.")

        sources = output.get("sources") or []
        if not sources:
            content = str(output.get("content") or "")
            url = str(output.get("url") or "")
            if content:
                sources = [{"url": url, "content": content}]

        for source in sources:
            if not isinstance(source, dict):
                continue
            result = classify_source_content(
                str(source.get("url") or ""),
                str(source.get("content") or ""),
            )
            if not result.is_public:
                return _CheckResult(
                    action="block",
                    triggered=True,
                    reason=f"source_not_public:{result.reason}",
                )
        return _CheckResult(action="allow", reason="Source content appears public.")

    def _check_role_required(self, context: GuardrailContext) -> _CheckResult:
        required = (context.tool_input or {}).get("_required_roles", [])
        if required and context.user_role not in required:
            return _CheckResult(
                action="block",
                triggered=True,
                reason=f"role_required:{required}",
            )
        return _CheckResult(action="allow", reason="Role requirement satisfied.")


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


def _policy_applies_to_context(
    applies_to: list[str],
    boundary: str,
    context: GuardrailContext,
) -> bool:
    if boundary not in applies_to:
        return False
    if not boundary.startswith("tool_"):
        return True

    tool_selectors = [selector for selector in applies_to if selector.startswith("tool:")]
    if not tool_selectors:
        return True
    if not context.tool_name:
        return False
    return f"tool:{context.tool_name}" in tool_selectors


def _query_template_ownership(db, template_id: str, user_id: str) -> bool:
    """Run the template ownership DB query against an already-open session."""

    return (
        db.query(DocumentTemplate)
        .filter(
            DocumentTemplate.public_id == template_id,
            DocumentTemplate.user_id == user_id,
            DocumentTemplate.is_active.is_(True),
        )
        .first()
        is not None
    )


def _template_belongs_to_user_db(template_id: str, user_id: str) -> bool:
    """Open a standalone session and check template ownership.

    This signature is kept for default guardrail lookups and tests. When a
    request-scoped session is available, pass a closure over
    _query_template_ownership to GuardrailService instead.
    """

    db = SessionLocal()
    try:
        return _query_template_ownership(db, template_id, user_id)
    finally:
        db.close()
