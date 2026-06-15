"""Brand design-system generator (#181).

Generates a new, first-class `design_system` entry for a user's uploaded
branded template by cloning a base spec (default `agentdeck_v1`) and
overlaying brand accent colors + fonts extracted into a `BrandProfile`
(#155). `slide_layouts` / `components` / spacing / elevation / radius are
left structurally identical to the base spec, so `layouts.js` /
`components.js` render the brand variant with zero code changes -- only
`color_tokens.{dark,light}.accent.*` (+ derived muted/chart tones) and
`typography.fontFace.{heading,body}` (+ matching scale entries) differ.

The result is written to `design_systems/<design_system_id>/spec.json`,
which `registry.list_design_systems()` / `get_design_system()` will then
discover like any built-in design system.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from ..brand.brand_profile import BrandProfile
from .agentdeck_v1.schema import DesignSystemSpec
from .registry import _PACKAGE_DIR, _load_spec

_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# color helpers
# ---------------------------------------------------------------------------


def _normalize_hex(value: str) -> str | None:
    candidate = str(value).strip().lstrip("#")
    return candidate.upper() if _HEX_RE.match(candidate) else None


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
    )


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "".join(f"{max(0, min(255, int(round(c)))):02X}" for c in rgb)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        c_norm = c / 255
        return c_norm / 12.92 if c_norm <= 0.03928 else ((c_norm + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    lum_a = _relative_luminance(_hex_to_rgb(hex_a))
    lum_b = _relative_luminance(_hex_to_rgb(hex_b))
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _saturation(rgb: tuple[int, int, int]) -> float:
    mx, mn = max(rgb), min(rgb)
    return 0.0 if mx == 0 else (mx - mn) / mx


def _is_neutral(rgb: tuple[int, int, int], threshold: int = 18) -> bool:
    return (max(rgb) - min(rgb)) <= threshold


def _adjust_for_contrast(hex_color: str, against_hex: str, min_ratio: float = 4.5) -> str:
    """Darken/lighten `hex_color` until it has >= `min_ratio` contrast against
    `against_hex` (e.g. the on-accent text color), bounded to a few steps so a
    badly-chosen brand color degrades gracefully instead of looping forever.
    """
    rgb = list(_hex_to_rgb(hex_color))
    darken = _relative_luminance(_hex_to_rgb(against_hex)) > 0.5
    for _ in range(12):
        if _contrast_ratio(_rgb_to_hex(tuple(rgb)), against_hex) >= min_ratio:
            break
        if darken:
            rgb = [c * 0.85 for c in rgb]
        else:
            rgb = [c + (255 - c) * 0.18 for c in rgb]
    return _rgb_to_hex(tuple(rgb))


def _muted(hex_color: str, theme: str) -> str:
    """Blend an accent color toward the theme's canvas to get a soft 'muted'
    chip/background tone, matching the role of `accent.*_muted` tokens.
    """
    rgb = _hex_to_rgb(hex_color)
    target = (20, 22, 28) if theme == "dark" else (255, 255, 255)
    blend = 0.78
    blended = tuple(c * (1 - blend) + t * blend for c, t in zip(rgb, target))
    return _rgb_to_hex(blended)


def pick_accent_colors(color_tokens: list[str], n: int = 2) -> list[str]:
    """Pick up to `n` non-neutral, highest-saturation colors from a
    BrandProfile's extracted `color_tokens`, most-saturated first.
    """
    candidates: list[str] = []
    for value in color_tokens:
        hex_color = _normalize_hex(value)
        if hex_color and not _is_neutral(_hex_to_rgb(hex_color)):
            candidates.append(hex_color)

    candidates.sort(key=lambda h: -_saturation(_hex_to_rgb(h)))

    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped[:n]


# ---------------------------------------------------------------------------
# id helpers
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("_", value.strip().lower()).strip("_")
    return slug or "user"


def design_system_id_for_template(user_id: str, template_public_id: str) -> str:
    """Deterministic design_system_id for a user's uploaded template, used as
    the on-disk directory name under `design_systems/`.
    """
    return f"brand_{_slug(user_id)}_{_slug(template_public_id)}"


# ---------------------------------------------------------------------------
# spec generation
# ---------------------------------------------------------------------------


def spec_from_brand_profile(
    brand_profile: BrandProfile,
    *,
    design_system_id: str,
    base: str = "agentdeck_v1",
) -> dict[str, Any]:
    """Build a new design-system spec dict by overlaying brand accent colors
    and fonts from `brand_profile` onto `base`'s spec.json.

    `slide_layouts`, `components`, `spacing`, `elevation`, `radius`,
    `generation_rules`, `token_pairs`, and `qa_thresholds` are copied
    unchanged from `base` -- only `color_tokens.*.accent`/`chart.series_{1,2}`
    and `typography.fontFace.{heading,body}` (+ matching scale entries) are
    overridden. The result is validated against `DesignSystemSpec` before
    being returned, so a malformed brand profile fails fast rather than
    producing a spec the renderer can't load.
    """
    base_path = _PACKAGE_DIR / base / "spec.json"
    base_data = json.loads(base_path.read_text())
    spec = copy.deepcopy(base_data)

    spec["meta"]["name"] = f"{base_data['meta']['name']} — Brand ({design_system_id})"
    spec["meta"]["description"] = (
        f"{base_data['meta']['description']} Brand variant generated from "
        f"uploaded template {brand_profile.source_template_id!r} (#181); "
        f"slide_layouts/components inherited verbatim from '{base}'."
    )

    accents = pick_accent_colors(brand_profile.color_tokens, n=2)
    if accents:
        primary = accents[0]
        secondary = accents[1] if len(accents) > 1 else primary
        for theme_name in ("dark", "light"):
            theme_tokens = spec["color_tokens"][theme_name]
            on_accent = theme_tokens["text"]["on_accent"]
            safe_primary = _adjust_for_contrast(primary, on_accent)
            safe_secondary = _adjust_for_contrast(secondary, on_accent)
            theme_tokens["accent"]["primary"] = safe_primary
            theme_tokens["accent"]["secondary"] = safe_secondary
            theme_tokens["accent"]["primary_muted"] = _muted(safe_primary, theme_name)
            theme_tokens["accent"]["secondary_muted"] = _muted(safe_secondary, theme_name)
            theme_tokens["chart"]["series_1"] = safe_primary
            theme_tokens["chart"]["series_2"] = safe_secondary

    fonts = [str(f).strip() for f in brand_profile.font_tokens if str(f).strip()]
    if fonts:
        base_heading = base_data["typography"]["fontFace"]["heading"]
        base_body = base_data["typography"]["fontFace"]["body"]
        heading_font = fonts[0]
        body_font = fonts[1] if len(fonts) > 1 else fonts[0]

        spec["typography"]["fontFace"]["heading"] = heading_font
        spec["typography"]["fontFace"]["body"] = body_font

        for style in spec["typography"]["scale"].values():
            if not isinstance(style, dict):
                continue
            if style.get("fontFace") == base_heading:
                style["fontFace"] = heading_font
            elif style.get("fontFace") == base_body:
                style["fontFace"] = body_font

    # #185: carry the extracted brand logo (if any) through to the spec so
    # `render_agentdeck.js` can place an `addLogoMark` overlay on each slide.
    # `meta` uses extra="allow", so this passes through DesignSystemSpec
    # validation as an additional field and survives `model_dump()`.
    if brand_profile.logo_assets:
        logo = brand_profile.logo_assets[0]
        if isinstance(logo, dict) and logo.get("data_base64"):
            spec["meta"]["brand_logo"] = {
                "content_type": logo.get("content_type") or "image/png",
                "data_base64": logo["data_base64"],
                "width_in": logo.get("width_in") or 1.2,
                "height_in": logo.get("height_in") or 0.6,
            }

    # Fail fast if the overlay produced something the registry can't load.
    DesignSystemSpec.model_validate(spec)
    return spec


def write_brand_design_system(
    brand_profile: BrandProfile,
    *,
    design_system_id: str,
    base: str = "agentdeck_v1",
) -> Path:
    """Generate and persist a brand design system, then bust the registry's
    spec cache so it's immediately available via `get_design_system()`.
    """
    spec = spec_from_brand_profile(brand_profile, design_system_id=design_system_id, base=base)
    out_dir = _PACKAGE_DIR / design_system_id
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=2))
    _load_spec.cache_clear()
    return spec_path
