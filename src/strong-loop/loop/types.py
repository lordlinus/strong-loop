"""
types.py — the platform's contracts.

Everything in the loop is one of these objects. If a concept
cannot be expressed here without adding a domain-specific field, it does not belong in
the core — it belongs in a charter, a grounding, or a gate.

The chain of custody runs:

    Accountability -> Question -> Hypothesis -> Evidence -> Finding
                                                              -> Decision -> Action -> Outcome

Two rules make the platform trustworthy, and they are enforced structurally rather than
by prompting:

  1. An agent may author a Hypothesis. It may NEVER author an Evidence. Evidence is
     minted only by gate code in `loop.gates`.
  2. An Action may only exist if its `action_type` is granted by the role charter's
     decision rights, and its supporting Finding cleared the evidence standard that the
     charter attaches to that action type.
"""

from __future__ import annotations

import datetime as _dt
import enum
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------------------
# Autonomy
# --------------------------------------------------------------------------------------
class AutonomyLevel(str, enum.Enum):
    """How much rope a role has for a given action type.

    A role is not "autonomous" or "not autonomous" — autonomy is granted per action
    class and earned by evidence in the outcome ledger. New action types start at
    L1_RECOMMEND and are promoted only after their outcome record justifies it.
    """

    L0_OBSERVE = "L0_observe"                 # may look, may not suggest
    L1_RECOMMEND = "L1_recommend"             # may suggest; a human executes
    L2_ACT_REVERSIBLE = "L2_act_reversible"   # may execute if trivially reversible
    L3_ACT_REPORT = "L3_act_report"           # may execute, must report after the fact
    L4_ACT = "L4_act"                         # may execute silently

    def rank(self) -> int:
        return _AUTONOMY_ORDER.index(self)


_AUTONOMY_ORDER = [
    AutonomyLevel.L0_OBSERVE,
    AutonomyLevel.L1_RECOMMEND,
    AutonomyLevel.L2_ACT_REVERSIBLE,
    AutonomyLevel.L3_ACT_REPORT,
    AutonomyLevel.L4_ACT,
]


# --------------------------------------------------------------------------------------
# Question graph — the derived workflow
# --------------------------------------------------------------------------------------
class Question(BaseModel):
    """A question the role should be answering.

    Derived from an accountability — never authored by a human, never hardcoded, and
    never chosen by the agent. The agent inherits what the role is accountable for.
    """

    id: str = Field(default_factory=lambda: _new_id("q"))
    accountability_id: str
    text: str
    why_it_matters: str = ""
    # Which measurable quantity in the grounding this question interrogates.
    target_measure: str | None = None
    priority: int = 5
    # `withheld` means a human narrowed this run's scope and removed the question. It is
    # kept in the record rather than dropped: a run that answered three of eleven
    # questions has not covered the accountability, and the report must be able to say so.
    status: Literal["open", "withheld", "exhausted", "answered"] = "open"
    created_at: str = Field(default_factory=_now)


class Hypothesis(BaseModel):
    """A falsifiable claim, authored by the agent, that a gate can test.

    `kind` selects the gate; `spec` is that gate's parameters. The agent must commit to
    both BEFORE seeing any result — this is what stops it from fishing for a test that
    happens to pass.
    """

    id: str = Field(default_factory=lambda: _new_id("h"))
    question_id: str
    statement: str
    kind: str                       # gate name, e.g. "proportion_lift"
    spec: dict[str, Any]            # gate-specific parameters
    rationale: str = ""
    author: str = "agent"           # which model / sub-agent proposed it
    created_at: str = Field(default_factory=_now)


class Verdict(str, enum.Enum):
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    REFUSED = "REFUSED"   # screen failure: PII, leakage, or out-of-scope columns


class Evidence(BaseModel):
    """The result of running a Hypothesis through a gate.

    NOTHING outside `loop.gates` may construct this. The agent receives it, reads it, and
    reasons about it — but cannot forge it and cannot argue with it.
    """

    id: str = Field(default_factory=lambda: _new_id("e"))
    hypothesis_id: str
    gate: str
    verdict: Verdict
    statistics: dict[str, Any] = Field(default_factory=dict)
    sample_size: int = 0
    effect_size: float | None = None
    p_value: float | None = None
    # Set later by the multiple-testing correction across a whole run.
    adjusted_p_value: float | None = None
    warnings: list[str] = Field(default_factory=list)
    refusal_reason: str | None = None
    evaluated_at: str = Field(default_factory=_now)


class Finding(BaseModel):
    """An interpreted, evidence-backed statement about the world."""

    id: str = Field(default_factory=lambda: _new_id("f"))
    hypothesis_id: str
    evidence_id: str
    question_id: str
    headline: str
    interpretation: str
    # Derived from evidence by code, never self-reported by the model.
    confidence: float = 0.0
    novelty: float = 0.0
    created_at: str = Field(default_factory=_now)


class Decision(BaseModel):
    """What the role concludes should happen, given one or more findings."""

    id: str = Field(default_factory=lambda: _new_id("d"))
    finding_ids: list[str]
    recommendation: str
    expected_effect: str = ""
    created_at: str = Field(default_factory=_now)


class Action(BaseModel):
    """A typed, permissioned intent to change something in the world.

    An Action is never free text. If the platform cannot name the tool, the blast radius,
    and how it will later observe the result, it is not allowed to act.
    """

    id: str = Field(default_factory=lambda: _new_id("a"))
    decision_id: str
    action_type: str                       # must appear in the charter's decision rights
    params: dict[str, Any] = Field(default_factory=dict)
    autonomy_level: AutonomyLevel = AutonomyLevel.L1_RECOMMEND
    reversible: bool = True
    blast_radius: dict[str, Any] = Field(default_factory=dict)
    # How and when we will find out whether this worked. No plan, no action.
    observation_plan: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "approved", "executed", "rejected", "failed"] = "proposed"
    created_at: str = Field(default_factory=_now)


class Outcome(BaseModel):
    """What actually happened. The only thing that turns iteration into learning."""

    id: str = Field(default_factory=lambda: _new_id("o"))
    action_id: str
    observed_at: str = Field(default_factory=_now)
    metrics: dict[str, Any] = Field(default_factory=dict)
    verdict: Literal["worked", "no_effect", "backfired", "unknown"] = "unknown"
    notes: str = ""
