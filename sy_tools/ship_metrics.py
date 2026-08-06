"""The `shipyard.ship_metrics.v1` machine-log shape, enforced where the log is posted.

`skills/ship/references/handoff-accounting.md` is the prose definition of every field below and the
only place their meanings are settled; this module is the same shape as a rejection, so a metrics
comment cannot land malformed and then be read as authoritative months later.

Validation happens at the tool boundary (`sy_tools/server.py`'s `post-comment`), not in a caller: a
check a caller performs is a check a caller can skip. A body carrying no `shipyard.ship_metrics.v1`
block is not this module's business and passes through untouched.

Every field but `schema`, `task`, `human_review_defects` and the two `pregate_checkpoint_*` fields is
optional: a metric genuinely unknown at ship time must be recordable as unknown rather than as a
plausible zero, and those exceptions are the ones a finished run always knows.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_ID = "shipyard.ship_metrics.v1"
"""The `schema` value that makes a fenced JSON block this model's to validate."""


class ShipMetricsV1(BaseModel):
    """One ship run's accounting record, as posted under `# Claude Code ship metrics`.

    Unknown keys are rejected rather than ignored, and the wire key `schema` is read into `schema_id`.
    """

    # `extra="forbid"`: a misspelled field would otherwise validate as an absent one — a record that
    # reads complete while the number someone wanted is missing.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Aliased because a field named `schema` shadows an attribute pydantic's `BaseModel` still carries
    # and warns about; the alias keeps the posted JSON as `handoff-accounting.md` documents it.
    schema_id: Literal["shipyard.ship_metrics.v1"] = Field(alias="schema")
    task: str
    pr_url: str | None = None
    plan_divergence_count: int | None = None
    deviations_declined: int | None = None
    ci_fix_rounds: int | None = None
    review_fix_rounds: int | None = None
    review_findings_accepted: int | None = None
    review_findings_rejected: int | None = None
    # Defaults to `0` and rejects an explicit `null`: "no human found anything" is a real observation at
    # ship time, where `null` would make a clean run indistinguishable from an unfinished record.
    human_review_defects: int = 0
    # Also never `null`: whether the plan declared a pre-gate checkpoint, and how many times that
    # checkpoint sent work back, are both settled by ship time, so `null` would hide an unfinished record.
    pregate_checkpoint_declared: bool = False
    pregate_checkpoint_changes_requested: int = 0
    gate_false_pass: bool | None = None
    gate_false_pass_reason: str | None = None
    post_merge_defect: bool | None = None
    rollback: bool | None = None
    lead_time_seconds: int | None = None
    transcript_attachment: str | None = None

    @model_validator(mode="after")
    def _task_is_not_blank(self) -> ShipMetricsV1:
        """A whitespace-only `task` is rejected: it passes a string check and names no issue."""
        if not self.task.strip():
            raise ValueError("'task' must name the issue this run shipped; it cannot be blank")
        return self

    @model_validator(mode="after")
    def _corrected_gate_verdict_carries_a_reason(self) -> ShipMetricsV1:
        """A non-null `gate_false_pass` must say why, per handoff-accounting.md's correction rule."""
        if self.gate_false_pass is not None and not (self.gate_false_pass_reason or "").strip():
            raise ValueError(
                "'gate_false_pass' was corrected to a verdict without a 'gate_false_pass_reason'; "
                "a correction has to say what the gate missed for the signal to be actionable"
            )
        return self

    @model_validator(mode="after")
    def _counts_are_not_negative(self) -> ShipMetricsV1:
        """No count field may be negative: a negative round or finding count is a computation bug."""
        counts = {
            "plan_divergence_count": self.plan_divergence_count,
            "deviations_declined": self.deviations_declined,
            "ci_fix_rounds": self.ci_fix_rounds,
            "review_fix_rounds": self.review_fix_rounds,
            "review_findings_accepted": self.review_findings_accepted,
            "review_findings_rejected": self.review_findings_rejected,
            "human_review_defects": self.human_review_defects,
            "pregate_checkpoint_changes_requested": self.pregate_checkpoint_changes_requested,
            "lead_time_seconds": self.lead_time_seconds,
        }
        negative = sorted(name for name, value in counts.items() if value is not None and value < 0)
        if negative:
            raise ValueError(f"these fields count things and cannot be negative: {', '.join(negative)}")
        return self
