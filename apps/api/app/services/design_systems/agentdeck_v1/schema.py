"""Pydantic schema for the AgentDeck Design System spec (agentdeck_v1).

This models `spec.json` 1:1 so it can be loaded, validated, and queried by
the registry, component renderers, and the planner/composer without any of
them needing to re-parse raw JSON or guess at shapes.

Color values are raw hex strings WITHOUT a leading '#' (PptxGenJS convention,
enforced by generation_rules.mandatory).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Theme = Literal["dark", "light"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


class DesignSystemMeta(_Base):
    name: str
    version: str
    target: str
    slide_layout: str
    slide_width_inches: float
    slide_height_inches: float
    themes: list[Theme]
    description: str


# ---------------------------------------------------------------------------
# color_tokens
# ---------------------------------------------------------------------------


class BgTokens(_Base):
    canvas: str
    surface_1: str
    surface_2: str
    surface_3: str
    overlay: str


class AccentTokens(_Base):
    primary: str
    primary_muted: str
    secondary: str
    secondary_muted: str
    gold: str
    gold_muted: str
    danger: str
    danger_muted: str
    success: str
    success_muted: str


class TextTokens(_Base):
    primary: str
    secondary: str
    muted: str
    on_accent: str
    # exactly one of these is present depending on theme
    on_dark_surface: Optional[str] = None
    on_light_surface: Optional[str] = None

    @property
    def on_surface(self) -> str:
        return self.on_dark_surface or self.on_light_surface or self.primary


class BorderTokens(_Base):
    subtle: str
    default: str
    strong: str


class ChartTokens(_Base):
    series_1: str
    series_2: str
    series_3: str
    series_4: str
    series_5: str
    grid: str

    @property
    def series(self) -> list[str]:
        return [self.series_1, self.series_2, self.series_3, self.series_4, self.series_5]


class ColorTokenSet(_Base):
    comment: Optional[str] = None
    bg: BgTokens
    accent: AccentTokens
    text: TextTokens
    border: BorderTokens
    chart: ChartTokens


class ColorTokens(_Base):
    dark: ColorTokenSet
    light: ColorTokenSet

    def for_theme(self, theme: Theme) -> ColorTokenSet:
        return self.dark if theme == "dark" else self.light


# ---------------------------------------------------------------------------
# typography
# ---------------------------------------------------------------------------


class FontFaces(_Base):
    heading: str
    body: str
    mono: str
    fallback: str


class TypeStyle(_Base):
    name: str
    usage: str
    fontSize_pt: float
    fontFace: str
    bold: bool = False
    lineSpacingMultiple: float = 1.0
    charSpacing: Optional[float] = None


class TypographyScale(_Base):
    display: TypeStyle
    h1: TypeStyle
    h2: TypeStyle
    h3: TypeStyle
    body: TypeStyle
    body_sm: TypeStyle
    label: TypeStyle
    stat: TypeStyle
    stat_sm: TypeStyle

    def get(self, name: str) -> TypeStyle:
        style = getattr(self, name, None)
        if style is None:
            raise KeyError(f"Unknown type scale token: {name}")
        return style


class Typography(_Base):
    fontFace: FontFaces
    scale: TypographyScale


# ---------------------------------------------------------------------------
# spacing
# ---------------------------------------------------------------------------


class SpacingTokens(_Base):
    xs: float
    sm: float
    md: float
    lg: float
    xl: float
    field_2xl: float = 0
    field_3xl: float = 0

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def get(self, name: str) -> float:
        # spec uses "2xl"/"3xl" keys which aren't valid python identifiers
        if name in ("2xl", "3xl"):
            extra = self.model_extra or {}
            return extra[name]
        return getattr(self, name)


class SlideMargins(_Base):
    top: float
    right: float
    bottom: float
    left: float
    content_top: float
    comment: Optional[str] = None


class ContentArea(_Base):
    x_start: float
    y_start: float
    width: float
    height: float


class Spacing(_Base):
    unit_inches: float
    tokens: SpacingTokens
    slide_margins: SlideMargins
    content_area: ContentArea


# ---------------------------------------------------------------------------
# elevation / radius
# ---------------------------------------------------------------------------


class ShadowSpec(_Base):
    type: str
    color: str
    blur: float
    offset: float
    angle: float
    opacity: float


class Elevation(_Base):
    none: Optional[ShadowSpec] = None
    card: ShadowSpec
    card_hover: ShadowSpec
    floating: ShadowSpec

    def get(self, name: str) -> Optional[ShadowSpec]:
        return getattr(self, name)


class Radius(_Base):
    none: float
    sm: float
    md: float
    lg: float
    pill: float

    def get(self, name: str) -> float:
        return getattr(self, name)


# ---------------------------------------------------------------------------
# grid
# ---------------------------------------------------------------------------


class GridPreset(_Base):
    cols: Optional[int] = None
    width: Optional[float] = None
    left: Optional[float] = None
    right: Optional[float] = None
    gutter: Optional[float] = None


class GridPresets(_Base):
    full: GridPreset
    half: GridPreset
    third: GridPreset
    quarter: GridPreset
    two_thirds: GridPreset
    one_third: GridPreset
    sidebar_L: GridPreset
    sidebar_R: GridPreset


class Grid(_Base):
    columns: int
    gutter: float
    margin: float
    usable_width: float
    col_width: float
    presets: GridPresets


# ---------------------------------------------------------------------------
# components / slide_layouts / generation_rules
# ---------------------------------------------------------------------------
#
# These two sections are intentionally kept as loosely-typed dicts: they are
# the part of the spec the planner/component-library treats as *data* (read
# by ComponentDef/registry.py, §3 of the architecture doc) rather than as a
# fixed schema Claude needs to validate field-by-field. Keeping them as
# pass-through dicts means new components/layouts can be added to spec.json
# without a schema migration.


class SlideLayoutZone(_Base):
    x: Optional[float] = None
    y: Optional[float] = None
    w: Optional[float] = None
    h: Optional[float] = None


class SlideLayout(_Base):
    name: str
    usage: str
    zones: dict[str, dict]


class SlideLayouts(_Base):
    """Wrapper for the `slide_layouts` block.

    The spec mixes metadata keys (`description`, `common_rules`) with named
    layout entries (TITLE, CONTENT_2COL, ...) at the same level. Metadata is
    modeled explicitly; layout entries land in `model_extra` and are accessed
    via `get()`/`names()`.
    """

    description: str
    common_rules: list[str]

    def names(self) -> list[str]:
        return sorted((self.model_extra or {}).keys())

    def get(self, name: str) -> SlideLayout:
        extra = self.model_extra or {}
        try:
            raw = extra[name]
        except KeyError as exc:
            raise KeyError(f"Unknown slide_layout {name!r}. Valid: {self.names()}") from exc
        return SlideLayout.model_validate(raw)


class GenerationRules(_Base):
    description: str
    mandatory: list[str]
    theme_switching: dict
    icon_handling: dict
    chart_colors: dict


# ---------------------------------------------------------------------------
# token_pairs (#136)
# ---------------------------------------------------------------------------
#
# Reusable semantic fill/text token groupings, resolved per-theme via
# color_tokens.{dark,light} through resolve_color()/resolve_token()
# (design_systems/registry.py). Each value is a dotted token path string
# (e.g. "accent.success_muted"), NOT a resolved hex color — resolution is
# theme-aware and happens at render/QA time. This generalizes the
# badge/callout_bar `variants` fill/text/icon pattern and the stat_card
# `delta` conditional color_token ("accent.success | accent.danger") into
# named pairs that stay correct under either theme.


class TokenPair(_Base):
    fill: str
    text: str
    solid_fill: Optional[str] = None
    solid_text: Optional[str] = None


class TokenPairConditional(_Base):
    """Maps a semantic condition (e.g. stat_card delta sign) to a
    `TokenPairs` key. `stat_delta` covers positive/negative/neutral deltas.
    """

    description: Optional[str] = None
    stat_delta: dict[str, str] = Field(default_factory=dict)


class TokenPairs(_Base):
    """Wrapper for the `token_pairs` block.

    Named pairs (`default`, `primary`, `success`, ...) land in `model_extra`
    since the set of semantic names is open-ended; `conditional` is modeled
    explicitly.
    """

    description: Optional[str] = None
    conditional: Optional[TokenPairConditional] = None

    def names(self) -> list[str]:
        return sorted((self.model_extra or {}).keys())

    def get(self, name: str) -> TokenPair:
        extra = self.model_extra or {}
        try:
            raw = extra[name]
        except KeyError as exc:
            raise KeyError(f"Unknown token_pairs entry {name!r}. Valid: {self.names()}") from exc
        return TokenPair.model_validate(raw)

    def resolve_conditional(self, group: str, condition: str) -> TokenPair:
        """e.g. resolve_conditional("stat_delta", "positive") -> TokenPair for 'success'."""
        if self.conditional is None:
            raise KeyError("No 'conditional' section in token_pairs")
        mapping = getattr(self.conditional, group, None)
        if not mapping:
            raise KeyError(f"Unknown token_pairs.conditional group {group!r}")
        try:
            pair_name = mapping[condition]
        except KeyError as exc:
            raise KeyError(
                f"Unknown condition {condition!r} for token_pairs.conditional.{group}"
            ) from exc
        return self.get(pair_name)


# ---------------------------------------------------------------------------
# qa_thresholds (#136)
# ---------------------------------------------------------------------------
#
# Numeric thresholds consumed by plan_checks.py (pre-render structural QA)
# and render_checks.py (post-render visual QA) — see #147. Theme-independent.


class ContrastThresholds(_Base):
    min_ratio_normal_text: float
    min_ratio_large_text: float
    large_text_pt: float


class MinFontPtThresholds(_Base):
    body: float
    body_sm: float
    label: float
    stat_sm: float


class MaxCharsThresholds(_Base):
    title: int
    dek: int
    bullet_item: int
    card_title: int
    card_body: int
    callout: int
    stat_label: int
    stat_caption: int
    table_cell: int


class MaxItemsThresholds(_Base):
    bullet_list: int
    card_grid: int
    stat_strip: int
    timeline: int
    table_rows: int
    decision_list: int


class WhitespaceThresholds(_Base):
    min_zone_fill_pct: float
    max_empty_zone_pct: float


class QAThresholds(_Base):
    description: Optional[str] = None
    contrast: ContrastThresholds
    min_font_pt: MinFontPtThresholds
    max_chars: MaxCharsThresholds
    max_items: MaxItemsThresholds
    whitespace: WhitespaceThresholds


# ---------------------------------------------------------------------------
# top-level spec
# ---------------------------------------------------------------------------


class DesignSystemSpec(_Base):
    meta: DesignSystemMeta
    color_tokens: ColorTokens
    typography: Typography
    spacing: Spacing
    elevation: Elevation
    radius: Radius
    grid: Grid
    components: dict[str, dict]
    slide_layouts: SlideLayouts
    generation_rules: GenerationRules
    token_pairs: TokenPairs
    qa_thresholds: QAThresholds

    # -- convenience accessors -------------------------------------------------

    def colors(self, theme: Theme) -> ColorTokenSet:
        return self.color_tokens.for_theme(theme)

    def type_style(self, name: str) -> TypeStyle:
        return self.typography.scale.get(name)

    def slide_layout(self, name: str) -> SlideLayout:
        return self.slide_layouts.get(name)

    def component(self, component_id: str) -> dict:
        try:
            return self.components[component_id]
        except KeyError as exc:
            raise KeyError(
                f"Unknown component {component_id!r}. Valid: {sorted(self.components)}"
            ) from exc

    def token_pair(self, name: str) -> TokenPair:
        return self.token_pairs.get(name)
