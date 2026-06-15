"""Component runtime protocol and default implementation (#140).

This module is intentionally small and deterministic. It gives Phase 2's
composer fit-validation step a stable interface without introducing another
LLM call or renderer dependency.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from .fit_contract import FitContract


class FitIssue(BaseModel):
    field: str
    message: str
    severity: str = "warning"


class FitResult(BaseModel):
    ok: bool
    density: float
    estimated_height_in: float | None = None
    issues: list[FitIssue] = Field(default_factory=list)


class ComponentRuntime(Protocol):
    def normalize(self, data: Any, model: type[BaseModel]) -> BaseModel:
        """Validate/coerce component props into the component content model."""

    def estimate_density(self, data: BaseModel, fit_contract: FitContract) -> float:
        """Return a 0..1 rough content-density score for the component."""

    def validate_fit(
        self,
        data: BaseModel,
        fit_contract: FitContract,
        *,
        zone_width_in: float | None = None,
        zone_height_in: float | None = None,
    ) -> FitResult:
        """Check component data against zone bounds and content caps."""


class DefaultComponentRuntime:
    def normalize(self, data: Any, model: type[BaseModel]) -> BaseModel:
        if isinstance(data, model):
            return data
        return model.model_validate(data)

    def estimate_density(self, data: BaseModel, fit_contract: FitContract) -> float:
        n_items = _item_count(data, fit_contract.item_field)
        ratios: list[float] = []
        if fit_contract.max_items:
            ratios.append(n_items / fit_contract.max_items)
        for field, max_chars in fit_contract.max_chars.items():
            longest = _max_text_len_for_field(data, field)
            if longest and max_chars:
                ratios.append(longest / max_chars)
        if not ratios:
            return 0.0
        return max(0.0, min(1.0, max(ratios)))

    def validate_fit(
        self,
        data: BaseModel,
        fit_contract: FitContract,
        *,
        zone_width_in: float | None = None,
        zone_height_in: float | None = None,
    ) -> FitResult:
        issues: list[FitIssue] = []
        n_items = _item_count(data, fit_contract.item_field)
        estimated_height = fit_contract.estimate_height_in(n_items)

        _check_bound(
            issues,
            "zone_width_in",
            zone_width_in,
            min_value=fit_contract.min_width_in,
            max_value=fit_contract.max_width_in,
        )
        _check_bound(
            issues,
            "zone_height_in",
            zone_height_in,
            min_value=fit_contract.min_height_in,
            max_value=fit_contract.max_height_in,
        )
        if fit_contract.exceeds_max_items(n_items):
            issues.append(
                FitIssue(
                    field=fit_contract.item_field or "items",
                    message=f"{n_items} items exceeds max {fit_contract.max_items}",
                    severity="error",
                )
            )
        if (
            zone_height_in is not None
            and estimated_height is not None
            and estimated_height > zone_height_in
        ):
            issues.append(
                FitIssue(
                    field="estimated_height_in",
                    message=(
                        f"estimated height {estimated_height:.2f}in exceeds "
                        f"zone height {zone_height_in:.2f}in"
                    ),
                    severity="error",
                )
            )
        for field, max_chars in fit_contract.max_chars.items():
            longest = _max_text_len_for_field(data, field)
            if longest > max_chars:
                issues.append(
                    FitIssue(
                        field=field,
                        message=f"{longest} chars exceeds max {max_chars}",
                        severity="warning",
                    )
                )

        return FitResult(
            ok=not any(issue.severity == "error" for issue in issues),
            density=self.estimate_density(data, fit_contract),
            estimated_height_in=estimated_height,
            issues=issues,
        )


DEFAULT_COMPONENT_RUNTIME = DefaultComponentRuntime()


def _check_bound(
    issues: list[FitIssue],
    field: str,
    value: float | None,
    *,
    min_value: float | None,
    max_value: float | None,
) -> None:
    if value is None:
        return
    if min_value is not None and value < min_value:
        issues.append(
            FitIssue(
                field=field,
                message=f"{value:.2f}in is below min {min_value:.2f}in",
                severity="error",
            )
        )
    if max_value is not None and value > max_value:
        issues.append(
            FitIssue(
                field=field,
                message=f"{value:.2f}in exceeds max {max_value:.2f}in",
                severity="warning",
            )
        )


def _item_count(data: BaseModel, field: str | None) -> int:
    if not field:
        return 0
    value = getattr(data, field, None)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1 if value is not None else 0


def _max_text_len_for_field(data: Any, field: str) -> int:
    values = _collect_field_values(data, field)
    if not values:
        values = _collect_field_values(data, _FIELD_ALIASES.get(field, field))
    max_len = 0
    for value in values:
        if value is None:
            continue
        max_len = max(max_len, len(str(value)))
    return max_len


def _collect_field_values(data: Any, field: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(data, BaseModel):
        raw = data.model_dump()
    else:
        raw = data
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key == field:
                found.append(value)
            found.extend(_collect_field_values(value, field))
    elif isinstance(raw, list):
        for item in raw:
            found.extend(_collect_field_values(item, field))
    return found


_FIELD_ALIASES = {
    "bullet_item": "text",
    "table_cell": "text",
    "step_label": "step_label",
}
