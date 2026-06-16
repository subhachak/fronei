from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.renderer.component_library import get_component


@dataclass(frozen=True)
class LayoutZone:
    id: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class FitIssue:
    issue_type: str
    message: str
    component_id: str | None = None
    zone_id: str | None = None


def validate_component_fit(
    component_id: str,
    zone: LayoutZone,
    *,
    item_count: int = 1,
    text_chars: int = 0,
) -> list[FitIssue]:
    component = get_component(component_id)
    if component is None:
        return [FitIssue("unknown_component", f"Unknown component {component_id}", component_id, zone.id)]

    issues: list[FitIssue] = []
    if zone.w < component.min_width:
        issues.append(FitIssue(
            "zone_too_narrow",
            f"{component_id} requires width {component.min_width}, got {zone.w}",
            component_id,
            zone.id,
        ))
    if zone.h < component.min_height:
        issues.append(FitIssue(
            "zone_too_short",
            f"{component_id} requires height {component.min_height}, got {zone.h}",
            component_id,
            zone.id,
        ))
    if item_count > component.max_items:
        issues.append(FitIssue(
            "too_many_items",
            f"{component_id} supports at most {component.max_items} items, got {item_count}",
            component_id,
            zone.id,
        ))
    if text_chars > max(80, int(zone.w * zone.h * 120)):
        issues.append(FitIssue(
            "text_density_high",
            f"{component_id} has high text density for zone {zone.id}",
            component_id,
            zone.id,
        ))
    return issues


def zone_from_dict(zone_id: str, data: dict[str, Any]) -> LayoutZone:
    return LayoutZone(
        id=zone_id,
        x=float(data.get("x", 0.0)),
        y=float(data.get("y", 0.0)),
        w=float(data.get("w", data.get("width", 0.0))),
        h=float(data.get("h", data.get("height", 0.0))),
    )
