"""AgentDeck v2 Designer-stage contracts (#157 stubs).

The implementation that *generates* these plans lands in Phase 3. Phase 2
only defines the stable object shape so QA/judges/repair can refer to design
intent explicitly.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .quality_mode import QualityMode
from .render_plan import SlideLayoutName

VisualRole = Literal[
    "hero",
    "section_divider",
    "analysis",
    "comparison",
    "evidence",
    "decision",
    "roadmap",
    "closing",
]
DensityTarget = Literal["sparse", "balanced", "dense"]
RepairConstraintType = Literal[
    "preserve_message",
    "preserve_evidence",
    "preserve_brand",
    "may_split_slide",
    "may_swap_component",
    "may_reduce_copy",
]


class RepairConstraint(BaseModel):
    type: RepairConstraintType
    note: Optional[str] = None


class SlideDesignTreatment(BaseModel):
    slide_id: Optional[str] = None
    visual_role: VisualRole = "analysis"
    layout_id: SlideLayoutName
    component_choices: dict[str, list[str]] = Field(default_factory=dict)
    hierarchy_notes: Optional[str] = None
    density_target: DensityTarget = "balanced"
    repair_constraints: list[RepairConstraint] = Field(default_factory=list)


class DesignPlan(BaseModel):
    design_system: str = "agentdeck_v1"
    theme: Literal["dark", "light"] = "dark"
    quality_mode: QualityMode = "standard"
    visual_direction: Optional[str] = None
    density_strategy: DensityTarget = "balanced"
    slide_treatments: list[SlideDesignTreatment] = Field(default_factory=list)

    # Phase 4 fills these with real models. They are present now so planner
    # prompts and QA signatures do not need another contract rewrite later.
    brand_profile_id: Optional[str] = None
    brand_profile: Optional[Any] = None
    user_document_profile: Optional[Any] = None
