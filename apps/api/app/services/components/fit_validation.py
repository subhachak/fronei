"""Pre-render component fit validation (#145).

Runs after planner content-schema validation and before the renderer. The
output is advisory for Phase 2: the composer attaches issues to slide notes
and leaves structural repair to the Phase 4 loop.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..design_systems.agentdeck_v1.schema import SlideLayout
from .registry import ComponentDef
from .runtime import DEFAULT_COMPONENT_RUNTIME, FitIssue, FitResult


class ComponentFitResult(BaseModel):
    slide_layout: str
    zone: str
    component_id: str
    ok: bool
    density: float
    estimated_height_in: float | None = None
    issues: list[FitIssue] = Field(default_factory=list)


def validate_component_fit(
    *,
    slide_layout: str,
    layout: SlideLayout,
    zone: str,
    component: ComponentDef,
    props: dict[str, Any],
) -> ComponentFitResult:
    zone_spec = layout.zones.get(zone) or {}
    data = DEFAULT_COMPONENT_RUNTIME.normalize(props, component.content_schema)
    result: FitResult = DEFAULT_COMPONENT_RUNTIME.validate_fit(
        data,
        component.fit_contract,
        zone_width_in=zone_spec.get("w"),
        zone_height_in=zone_spec.get("h"),
    )
    return ComponentFitResult(
        slide_layout=slide_layout,
        zone=zone,
        component_id=component.id,
        ok=result.ok,
        density=result.density,
        estimated_height_in=result.estimated_height_in,
        issues=result.issues,
    )


def format_fit_issues(results: list[ComponentFitResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        if result.ok and not result.issues:
            continue
        for issue in result.issues:
            lines.append(
                f"{result.zone}/{result.component_id}: {issue.severity}: "
                f"{issue.field} - {issue.message}"
            )
    return lines
