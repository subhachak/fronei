"""Design-system registry.

Loads/caches design-system specs (currently just `agentdeck_v1`) and exposes
token-resolution helpers shared by the Composer (Python) and, indirectly,
by `render.js` (which receives the fully-resolved spec as JSON and resolves
its own tokens at render time per `generation_rules.theme_switching`).

Adding a new design system later = drop a new `<id>/spec.json` (+ optional
`schema.py` if its shape differs) and register a loader below.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .agentdeck_v1.schema import DesignSystemSpec, ShadowSpec, Theme, TypeStyle

_PACKAGE_DIR = Path(__file__).parent

DEFAULT_DESIGN_SYSTEM = "agentdeck_v1"


@lru_cache(maxsize=None)
def _load_spec(design_system_id: str) -> DesignSystemSpec:
    spec_path = _PACKAGE_DIR / design_system_id / "spec.json"
    if not spec_path.exists():
        raise KeyError(
            f"Unknown design system {design_system_id!r} "
            f"(expected {spec_path})"
        )
    data = json.loads(spec_path.read_text())
    return DesignSystemSpec.model_validate(data)


def list_design_systems() -> list[str]:
    return sorted(
        p.name
        for p in _PACKAGE_DIR.iterdir()
        if p.is_dir() and (p / "spec.json").exists()
    )


def get_design_system(design_system_id: str = DEFAULT_DESIGN_SYSTEM) -> DesignSystemSpec:
    """Return the (cached, validated) spec for `design_system_id`."""
    return _load_spec(design_system_id)


def design_system_payload(design_system_id: str = DEFAULT_DESIGN_SYSTEM) -> dict[str, Any]:
    """Full JSON-serializable spec, as handed to `render.js`."""
    return get_design_system(design_system_id).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------
#
# Token references are dotted strings, namespaced by which top-level spec
# section they resolve against:
#
#   color tokens   -> "bg.canvas", "accent.primary", "text.on_accent",
#                      "border.subtle", "chart.series_1"   (theme-dependent)
#   type styles    -> "type.h1", "type.stat", "type.body_sm"
#   spacing        -> "spacing.md", "spacing.2xl"
#   radius         -> "radius.pill"
#   elevation      -> "elevation.card"
#
# `text.on_surface` is a synthetic token resolving to whichever of
# on_dark_surface/on_light_surface the active theme defines.

_COLOR_GROUPS = {"bg", "accent", "text", "border", "chart"}


def resolve_color(spec: DesignSystemSpec, theme: Theme, token: str) -> str:
    """Resolve a color token like 'accent.primary' for the given theme.

    Returns a raw hex string (no leading '#'), per generation_rules.
    """
    group, _, key = token.partition(".")
    if group not in _COLOR_GROUPS or not key:
        raise ValueError(f"Not a color token: {token!r}")

    colors = spec.colors(theme)
    group_obj = getattr(colors, group)

    if group == "text" and key == "on_surface":
        return colors.text.on_surface

    if key == "series":
        # convenience: 'chart.series' -> list of series colors
        return colors.chart.series  # type: ignore[return-value]

    try:
        return getattr(group_obj, key)
    except AttributeError as exc:
        raise ValueError(f"Unknown {group} token: {key!r}") from exc


def resolve_type(spec: DesignSystemSpec, name: str) -> TypeStyle:
    """Resolve a type-scale token, e.g. 'type.h1' or bare 'h1'."""
    _, _, key = name.rpartition(".")
    return spec.type_style(key)


def resolve_spacing(spec: DesignSystemSpec, name: str) -> float:
    """Resolve a spacing token, e.g. 'spacing.lg' or 'spacing.2xl'."""
    _, _, key = name.rpartition(".")
    return spec.spacing.tokens.get(key)


def resolve_radius(spec: DesignSystemSpec, name: str) -> float:
    _, _, key = name.rpartition(".")
    return spec.radius.get(key)


def resolve_shadow(spec: DesignSystemSpec, name: str) -> ShadowSpec | None:
    _, _, key = name.rpartition(".")
    return spec.elevation.get(key)


def resolve_token(spec: DesignSystemSpec, theme: Theme, token: str) -> Any:
    """Generic dispatch for any namespaced token reference."""
    namespace, _, _ = token.partition(".")
    if namespace in _COLOR_GROUPS:
        return resolve_color(spec, theme, token)
    if namespace == "type":
        return resolve_type(spec, token)
    if namespace == "spacing":
        return resolve_spacing(spec, token)
    if namespace == "radius":
        return resolve_radius(spec, token)
    if namespace == "elevation":
        return resolve_shadow(spec, token)
    raise ValueError(f"Unrecognized token namespace in {token!r}")
