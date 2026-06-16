from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComponentDefinition:
    id: str
    min_width: float = 1.0
    min_height: float = 0.5
    max_items: int = 8
    required_props: tuple[str, ...] = ()
    variants: tuple[str, ...] = ()


DEFAULT_COMPONENTS: dict[str, ComponentDefinition] = {
    "title": ComponentDefinition("title", min_width=4.0, min_height=0.4, required_props=("text",)),
    "dek": ComponentDefinition("dek", min_width=4.0, min_height=0.25, required_props=("text",)),
    "card_grid": ComponentDefinition("card_grid", min_width=3.0, min_height=1.2, max_items=6),
    "stat_card": ComponentDefinition("stat_card", min_width=1.2, min_height=0.8, max_items=4),
    "table": ComponentDefinition("table", min_width=4.0, min_height=1.4, max_items=12),
    "timeline": ComponentDefinition("timeline", min_width=5.0, min_height=1.2, max_items=6),
    "risk_matrix": ComponentDefinition("risk_matrix", min_width=4.0, min_height=2.0, max_items=9),
    "callout": ComponentDefinition("callout", min_width=3.0, min_height=0.6),
}


def get_component(component_id: str) -> ComponentDefinition | None:
    return DEFAULT_COMPONENTS.get(component_id)


def validate_component_props(component_id: str, props: dict[str, Any]) -> list[str]:
    component = get_component(component_id)
    if component is None:
        return [f"unknown_component:{component_id}"]
    missing = [name for name in component.required_props if not props.get(name)]
    return [f"missing_prop:{name}" for name in missing]
