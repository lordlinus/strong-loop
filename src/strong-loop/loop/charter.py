"""
charter.py — the Role Charter: a role expressed as a contract, not a prompt.

This is the platform's primary input and the reason it is a platform rather than a
solution. A charter says what a role is ACCOUNTABLE for, what it MAY DO, what counts as
PROOF, and what it MUST NOT touch. It deliberately cannot express:

  - questions to ask        (one per accountability, derived in `questions.py`)
  - analyses to run         (chosen by the agent)
  - workflows or screens    (emergent)

If you find yourself wanting to add any of those to a charter, the platform has sprung a
leak and is turning back into a bespoke solution. That is the single most useful
invariant in this repo, so `validate_charter` enforces it explicitly.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .types import Action, AutonomyLevel, Evidence, Finding, Verdict


class Accountability(BaseModel):
    """An outcome the role owns, expressed as a measurable quantity and a direction.

    The `metric` must be a column of the dataset the role runs against. This is what
    forces charters to be honest: "improve customer relationships" cannot be written here,
    because there is no measure to point at.
    """

    id: str
    statement: str
    metric: str
    direction: Literal["increase", "decrease", "stabilise"]
    horizon_days: int = 90
    priority: int = 5

    @field_validator("metric")
    @classmethod
    def _metric_is_an_identifier(cls, v: str) -> str:
        if not v or " " in v.strip():
            raise ValueError(
                f"accountability metric must be a single measure identifier, got {v!r}. "
                "Prose goals belong in `statement`; `metric` must name a data column."
            )
        return v


class BlastRadius(BaseModel):
    """The maximum damage a single action type can do in one run."""

    unit: str = "records"
    max_per_run: int = 100
    max_value_at_risk: float | None = None


class DecisionRight(BaseModel):
    """One thing this role is permitted to do, and how much rope it has.

    Actions the charter does not name simply cannot be taken. There is no escape hatch —
    an agent that invents an action type gets a hard refusal from `authorise_action`.
    """

    action_type: str
    description: str = ""
    autonomy_level: AutonomyLevel = AutonomyLevel.L1_RECOMMEND
    reversible: bool = True
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    requires_authority: str | None = None
    # Which observation the platform will use to score this action later. An action type
    # with no observation metric can never be promoted above L1, because nothing will
    # ever prove it works.
    observation_metric: str | None = None
    observation_lag_days: int = 30


class EvidenceStandard(BaseModel):
    """What counts as proof. Different actions warrant different burdens of proof.

    Flagging a customer for a phone call and changing a pricing factor are not the same
    decision, and should not clear the same bar.
    """

    min_sample_size: int = 100
    max_p_value: float = 0.05
    min_effect_size: float = 1.2
    multiple_testing_correction: Literal["benjamini_hochberg", "bonferroni", "none"] = (
        "benjamini_hochberg"
    )
    requires_causal_design: bool = False
    requires_holdout: bool = False
    # A subgroup claim may not become a finding until somebody has tried to break it by
    # holding a candidate confound constant. Defaults ON: the traps this catches are the
    # ones nobody anticipated, so opting IN would only ever protect the scenarios we
    # already thought about. A charter may set this false, but that is then a visible,
    # auditable decision rather than an omission.
    requires_confound_control: bool = True


class Constraints(BaseModel):
    """What the role may not touch, regardless of what it discovers."""

    # Columns that may never appear in a hypothesis, even if predictive.
    forbidden_features: list[str] = Field(default_factory=list)
    # Columns that would leak the outcome being predicted into its own predictor.
    leakage_features: list[str] = Field(default_factory=list)
    # If set, hypotheses may reference ONLY these columns.
    allowed_features: list[str] | None = None
    block_sensitive_columns: bool = True
    notes: str = ""


class RoleCharter(BaseModel):
    role: str
    version: int = 1
    description: str = ""
    extends: str | None = None
    accountabilities: list[Accountability]
    decision_rights: list[DecisionRight] = Field(default_factory=list)
    evidence_standards: EvidenceStandard = Field(default_factory=EvidenceStandard)
    # Per-action-type overrides of the default evidence standard.
    evidence_overrides: dict[str, EvidenceStandard] = Field(default_factory=dict)
    constraints: Constraints = Field(default_factory=Constraints)

    # ---- signature ------------------------------------------------------------------
    # An unsigned charter loads and runs; a signed one is also checked for tampering.
    # `sign()` fills these in; nothing else writes them.
    status: Literal["DRAFT", "SIGNED"] = "SIGNED"
    ratified_by: str | None = None
    ratified_at: str | None = None
    content_hash: str | None = None

    # ---- lookups -------------------------------------------------------------------
    def right(self, action_type: str) -> DecisionRight | None:
        return next((r for r in self.decision_rights if r.action_type == action_type), None)

    def standard_for(self, action_type: str | None = None) -> EvidenceStandard:
        if action_type and action_type in self.evidence_overrides:
            return self.evidence_overrides[action_type]
        return self.evidence_standards

    def accountability(self, accountability_id: str) -> Accountability | None:
        return next((a for a in self.accountabilities if a.id == accountability_id), None)

    def fingerprint(self) -> str:
        """A stable hash of the parts that decide what may happen.

        Recorded on every run so a finding can be traced to the exact authority it was
        produced under. Deliberately excludes the signature fields themselves — otherwise
        the hash would change when it is written back into the object it describes.
        """
        payload = self.model_dump(
            mode="json",
            exclude={"status", "ratified_by", "ratified_at", "content_hash"},
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


# --------------------------------------------------------------------------------------
# Loading + the anti-leak validator
# --------------------------------------------------------------------------------------

# Keys that signal a charter is drifting back into being a hardcoded solution.
_SOLUTION_SMELLS = {
    "questions", "workflow", "workflows", "steps", "screens", "pages", "dashboards",
    "analyses", "reports", "segments", "cohorts", "campaigns", "playbook", "sql",
}


def validate_charter_shape(raw: dict[str, Any]) -> list[str]:
    """Reject charters that encode a workflow instead of a job.

    Returns a list of problems. This runs on the RAW dict rather than the parsed model,
    because the whole point is to catch keys that should not exist at all.
    """
    problems: list[str] = []
    for key in raw:
        if key.lower() in _SOLUTION_SMELLS:
            problems.append(
                f"charter defines `{key}` — that is a solution detail the platform must "
                f"derive, not an input. Remove it."
            )
    for acc in raw.get("accountabilities", []) or []:
        if isinstance(acc, dict) and "questions" in acc:
            problems.append(
                f"accountability `{acc.get('id')}` lists questions. Questions are compiled "
                f"from the accountability and the data, never supplied."
            )
    return problems


def load_charter(path: str | pathlib.Path, *, verify: bool = True) -> RoleCharter:
    """Load and validate a charter from YAML, applying `extends` inheritance.

    `verify=False` is for the one caller that legitimately needs to read a charter whose
    signature no longer matches: `python -m loop sign`, which exists to produce a new one.
    Everything that *acts* on a charter must leave it True.
    """
    path = pathlib.Path(path)
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not contain a charter mapping")

    problems = validate_charter_shape(raw)
    if problems:
        raise ValueError("invalid charter:\n  - " + "\n  - ".join(problems))

    parent_name = raw.get("extends")
    if parent_name:
        parent_path = path.parent / f"{parent_name}.yaml"
        if not parent_path.exists():
            raise FileNotFoundError(f"charter {path.name} extends missing base {parent_path}")
        parent_raw = yaml.safe_load(parent_path.read_text())
        raw = _merge_charter(parent_raw, raw)

    charter = RoleCharter.model_validate(raw)

    # A ratification signs specific content. Recording the hash and never checking it
    # makes it decorative: a signed charter could be edited afterwards and nothing would
    # notice. That matters most for `decision_rights`, because editing those is precisely
    # how a role would acquire a capability no human approved.
    if charter.content_hash and verify and charter.fingerprint() != charter.content_hash:
        raise ValueError(
            f"charter {path.name} has been modified since it was ratified by "
            f"{charter.ratified_by or 'unknown'} — recorded {charter.content_hash[:12]}, "
            f"now {charter.fingerprint()[:12]}. Re-sign it with `python -m loop sign`."
        )
    return charter


def _merge_charter(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Child wins on scalars; lists of identified objects merge by id/action_type.

    Inheritance is what makes charters reusable across markets and business units: define
    `analyst` once, then let `underwriting_analyst` add rights without restating the base.
    """
    merged = dict(parent)
    for key, value in child.items():
        if key == "extends":
            continue
        if isinstance(value, list) and isinstance(parent.get(key), list):
            id_key = "id" if key == "accountabilities" else "action_type"
            by_id = {
                item[id_key]: item
                for item in parent[key]
                if isinstance(item, dict) and id_key in item
            }
            for item in value:
                if isinstance(item, dict) and id_key in item:
                    by_id[item[id_key]] = item
            merged[key] = list(by_id.values())
        elif isinstance(value, dict) and isinstance(parent.get(key), dict):
            merged[key] = {**parent[key], **value}
        else:
            merged[key] = value
    return merged


def sign(charter: RoleCharter, ratified_by: str) -> RoleCharter:
    """Record who ratified this exact content. The hash is what `load_charter` checks."""
    signed = charter.model_copy(deep=True)
    signed.status = "SIGNED"
    signed.ratified_by = ratified_by
    signed.ratified_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    signed.content_hash = signed.fingerprint()
    return signed


def write_charter(charter: RoleCharter, path: str | pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = charter.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


# ======================================================================================
# Authorisation — may this role take this action?
# ======================================================================================
#
# The counterpart to the evidence gate. The gate decides whether something is TRUE; this
# decides whether the role may ACT on it. Both are fixed code, for the same reason: an
# autonomous system that can talk itself into either one is not governable.


@dataclass
class Authorisation:
    granted: bool
    reasons: list[str]
    autonomy_level: AutonomyLevel | None = None
    requires_authority: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "reasons": self.reasons,
            "autonomy_level": self.autonomy_level.value if self.autonomy_level else None,
            "requires_authority": self.requires_authority,
        }


def authorise_action(
    *,
    charter: RoleCharter,
    action_type: str,
    findings: list[Finding],
    evidence_by_id: dict[str, Evidence],
    requested_blast_radius: dict[str, Any] | None = None,
    observation_plan: dict[str, Any] | None = None,
) -> Authorisation:
    """An action is authorised only if the charter grants the type, every supporting
    finding clears the standard for THAT type, the blast radius is within cap, and there
    is a plan to observe the result."""
    right = charter.right(action_type)
    if right is None:
        granted = [r.action_type for r in charter.decision_rights]
        return Authorisation(
            granted=False,
            reasons=[
                f"action type {action_type!r} is not in this role's decision rights. "
                f"Granted: {granted or '(none)'}."
            ],
        )

    if not findings:
        return Authorisation(
            granted=False,
            reasons=["no supporting findings — actions must be evidence-backed"],
            autonomy_level=right.autonomy_level,
        )

    blockers: list[str] = []
    notes: list[str] = []
    standard = charter.standard_for(action_type)

    for finding in findings:
        evidence = evidence_by_id.get(finding.evidence_id)
        if evidence is None:
            blockers.append(f"finding {finding.id} has no evidence record")
            continue
        if evidence.verdict != Verdict.SUPPORTED:
            blockers.append(f"finding {finding.id} rests on {evidence.verdict.value} evidence")
            continue
        # Prefer the corrected p-value; an autonomous loop runs enough tests that the raw
        # value is not a defensible basis for acting.
        p = evidence.adjusted_p_value if evidence.adjusted_p_value is not None else evidence.p_value
        if p is None or p >= standard.max_p_value:
            blockers.append(
                f"finding {finding.id} p={p} does not clear the {action_type} standard "
                f"of p<{standard.max_p_value}"
            )
        if evidence.sample_size < standard.min_sample_size:
            blockers.append(
                f"finding {finding.id} n={evidence.sample_size} is below the "
                f"{action_type} minimum of {standard.min_sample_size}"
            )
        if standard.requires_causal_design:
            blockers.append(
                f"{action_type} requires a causal design; gate {evidence.gate!r} is "
                f"observational only"
            )

    if requested_blast_radius:
        requested = requested_blast_radius.get("max_per_run")
        cap = right.blast_radius.max_per_run
        if requested is not None and requested > cap:
            blockers.append(
                f"requested blast radius {requested} exceeds the charter cap of {cap} "
                f"{right.blast_radius.unit}"
            )

    if not observation_plan or not observation_plan.get("metric"):
        blockers.append(
            "no observation plan — an action whose effect is never measured can never be "
            "learned from, so it is not permitted"
        )

    # An action type with no observation metric can never earn promotion out of L1,
    # whatever the charter nominally grants.
    effective = right.autonomy_level
    if right.observation_metric is None and effective.rank() > AutonomyLevel.L1_RECOMMEND.rank():
        effective = AutonomyLevel.L1_RECOMMEND
        notes.append(
            f"{action_type} declares no observation_metric, so autonomy is capped at "
            f"L1_recommend regardless of the charter's grant"
        )

    return Authorisation(
        granted=not blockers,
        reasons=blockers + notes or ["all charter conditions satisfied"],
        autonomy_level=effective,
        requires_authority=right.requires_authority,
    )


def apply_authorisation(action: Action, auth: Authorisation, reversible: bool) -> Action:
    """Stamp the authorisation outcome onto the action."""
    action.autonomy_level = auth.autonomy_level or AutonomyLevel.L1_RECOMMEND
    action.reversible = reversible
    if not auth.granted:
        action.status = "rejected"
    elif action.autonomy_level.rank() >= AutonomyLevel.L2_ACT_REVERSIBLE.rank():
        action.status = "approved"
    else:
        action.status = "proposed"
    return action
