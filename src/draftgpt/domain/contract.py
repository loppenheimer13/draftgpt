"""The common command response contract (brief section 8).

Every command returns this shape. Structured JSON is the primary artifact; the
rendered text is derived from it, never the other way around.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FreshnessStatus = Literal["fresh", "degraded", "stale"]
Urgency = Literal["critical", "high", "normal", "low"]


class SourceFreshnessReport(BaseModel):
    provider: str
    capability: str
    freshness_class: str
    last_success_at: datetime | None = None
    effective_at: datetime | None = None
    age_seconds: float | None = None
    max_age_seconds: float | None = None
    status: FreshnessStatus = "fresh"
    health: str = "unknown"
    note: str | None = None


class FreshnessBlock(BaseModel):
    status: FreshnessStatus = "fresh"
    warnings: list[str] = Field(default_factory=list)
    sources: list[SourceFreshnessReport] = Field(default_factory=list)


class MaterialInput(BaseModel):
    """A fact the recommendation actually depended on.

    The explanation layer may only reference these. Anything absent here is, by
    construction, not something the model is permitted to assert.
    """

    key: str
    label: str
    value: Any = None
    provider: str | None = None
    effective_at: datetime | None = None
    weight: float | None = None


class Condition(BaseModel):
    """A conditional trigger: what to do if the world changes before kickoff."""

    trigger: str
    action: str
    check_by: datetime | None = None
    urgency: Urgency = "normal"


class ActionItem(BaseModel):
    action: str
    detail: str | None = None
    urgency: Urgency = "normal"
    deeper_command: str | None = None


class Candidate(BaseModel):
    rank: int
    label: str
    subject_type: str = "player"
    subject_id: str | None = None
    score: float | None = None
    projected_points: float | None = None
    floor_points: float | None = None
    ceiling_points: float | None = None
    value_over_replacement: float | None = None
    survival_probability: float | None = None
    tier: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    command: str
    subcommand: str | None = None
    league_id: str
    snapshot_id: str
    run_id: str | None = None
    generated_at: datetime
    calculation_version: str
    week: int | None = None

    recommendation: dict[str, Any] = Field(default_factory=dict)
    alternatives: list[Candidate] = Field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    material_inputs: list[MaterialInput] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    freshness: FreshnessBlock = Field(default_factory=FreshnessBlock)
    do_now: list[ActionItem] = Field(default_factory=list)
    watch: list[ActionItem] = Field(default_factory=list)
    check_again_at: datetime | None = None
    explanation: str | None = None

    def material_input_keys(self) -> set[str]:
        return {mi.key for mi in self.material_inputs}
