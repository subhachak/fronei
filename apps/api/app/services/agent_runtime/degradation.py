from __future__ import annotations

from enum import Enum

from app.services.agent_runtime.circuit_breaker import CircuitBreakerRegistry, CircuitState


class DegradationTier(str, Enum):
    FULL = "full"
    DEGRADED_RESEARCH = "degraded_research"
    DEGRADED_DOCUMENT = "degraded_document"
    MINIMAL = "minimal"


def resolve_tier() -> DegradationTier:
    registry = CircuitBreakerRegistry.get()
    research_open = registry.breaker("tool:web_search").state == CircuitState.OPEN
    doc_open = registry.breaker("tool:generate_document").state == CircuitState.OPEN
    llm_open = any(
        breaker.state == CircuitState.OPEN
        for key, breaker in registry.items()
        if key.startswith("llm:")
    )
    if llm_open:
        return DegradationTier.MINIMAL
    if research_open and doc_open:
        return DegradationTier.MINIMAL
    if research_open:
        return DegradationTier.DEGRADED_RESEARCH
    if doc_open:
        return DegradationTier.DEGRADED_DOCUMENT
    return DegradationTier.FULL
