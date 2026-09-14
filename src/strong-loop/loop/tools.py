"""tools.py — the governed operations, bound to one run's charter, data and ledger.

Eight tools. Five read; three write, and the three that write are the only doors through
which a Finding, a Decision or an Action can come into existence — `tests/
test_invariant_evidence.py` refuses a fourth. `test_hypothesis` is the central one: the
agent supplies the claim and the test parameters and does not supply, and cannot influence,
the verdict. `gates.evaluate` mints the Evidence; this module only records it.

Nothing here knows an agent exists. `runner.py` wraps these methods as tools per run.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import gates
from .charter import RoleCharter, apply_authorisation, authorise_action
from .gates import GateContext, confidence_from
from .ledger import Ledger, fingerprint
from .types import Action, Decision, Evidence, Finding, Hypothesis, Question, Verdict


def profile(data: pd.DataFrame, charter: RoleCharter) -> dict[str, Any]:
    """What the data looks like, and the level of every accountability's metric today.

    Descriptive facts only: no verdict, nothing to overturn, nothing to self-certify.
    Sensitive columns are named as excluded and not described. The base rate / mean for
    each metric is here because the first question about any accountability is "what is
    the level today", and every gate needs a subgroup to compare against; without it the
    agent burns an iteration finding out.
    """
    columns: dict[str, Any] = {}
    excluded: list[str] = []
    blocked = set(charter.constraints.forbidden_features)
    for name in data.columns:
        if gates.is_sensitive_column(name) or name in blocked:
            excluded.append(name)
            continue
        series = data[name]
        info: dict[str, Any] = {
            "dtype": str(series.dtype),
            "null_share": round(float(series.isna().mean()), 4),
            "n_unique": int(series.nunique()),
        }
        if pd.api.types.is_numeric_dtype(series) and series.nunique() > 2:
            desc = series.describe()
            info["range"] = [round(float(desc["min"]), 4), round(float(desc["max"]), 4)]
            info["median"] = round(float(series.median()), 4)
        else:
            info["values"] = [str(v) for v in series.dropna().unique()[:8]]
        columns[name] = info

    measures: dict[str, Any] = {}
    for acc in charter.accountabilities:
        if acc.metric not in data.columns:
            measures[acc.metric] = {"status": "not in this data"}
            continue
        numeric = pd.to_numeric(data[acc.metric], errors="coerce").dropna()
        if numeric.empty:
            measures[acc.metric] = {"status": "no numeric values"}
        elif set(pd.unique(numeric)).issubset({0, 1}):
            measures[acc.metric] = {
                "kind": "rate", "base_rate": round(float(numeric.mean()), 6),
                "positives": int(numeric.sum()), "n": int(len(numeric)),
            }
        else:
            measures[acc.metric] = {
                "kind": "numeric", "mean": round(float(numeric.mean()), 4),
                "median": round(float(numeric.median()), 4), "n": int(len(numeric)),
            }
    return {
        "rows": int(len(data)),
        "columns": columns,
        "excluded_columns": excluded,
        "leakage_columns": list(charter.constraints.leakage_features),
        "accountability_measures": measures,
    }


class Toolbelt:
    """The bound set of operations for one run."""

    def __init__(self, charter: RoleCharter, data: pd.DataFrame, ledger: Ledger):
        self.charter = charter
        self.data = data
        self.ledger = ledger

    def tools(self) -> list:
        from agent_framework import tool

        return [
            tool(self.get_charter), tool(self.get_data_profile), tool(self.list_gates),
            tool(self.list_questions), tool(self.get_progress),
            tool(self.test_hypothesis), tool(self.record_finding), tool(self.propose_action),
        ]

    # ---- read -----------------------------------------------------------------------
    def get_charter(self) -> dict[str, Any]:
        """What this role is accountable for, may do, and must not touch."""
        return {
            "role": self.charter.role,
            "description": self.charter.description,
            "accountabilities": [a.model_dump() for a in self.charter.accountabilities],
            "decision_rights": [
                {
                    "action_type": r.action_type, "description": r.description,
                    "autonomy_level": r.autonomy_level.value,
                    "max_per_run": r.blast_radius.max_per_run, "unit": r.blast_radius.unit,
                }
                for r in self.charter.decision_rights
            ],
            "evidence_standard": self.charter.evidence_standards.model_dump(),
            "constraints": self.charter.constraints.model_dump(),
        }

    def get_data_profile(self) -> dict[str, Any]:
        """The columns, their shapes, and today's level of each accountability metric."""
        return profile(self.data, self.charter)

    def list_gates(self) -> list[dict[str, Any]]:
        """The shapes of question this loop can settle, with each gate's spec schema.

        Pick one. If none fits the idea, that is a missing gate — an engineering outcome,
        not a prompt problem.
        """
        return gates.available()

    def list_questions(self, only_open: bool = True) -> list[dict[str, Any]]:
        """The open questions this role inherited from its charter, by priority."""
        questions = self.ledger.all("question")
        if only_open:
            questions = [q for q in questions if q.status == "open"]
        return [
            {
                "id": q.id, "text": q.text, "why_it_matters": q.why_it_matters,
                "accountability_id": q.accountability_id, "priority": q.priority,
            }
            for q in sorted(questions, key=lambda q: q.priority)
        ]

    def get_progress(self) -> dict[str, Any]:
        """What has already been tried this run. Read this before proposing anything."""
        return self.ledger.summary()

    # ---- the gate call --------------------------------------------------------------
    def test_hypothesis(
        self, question_id: str, statement: str, kind: str, spec: dict[str, Any], rationale: str = ""
    ) -> dict[str, Any]:
        """Submit a falsifiable claim for testing. THE central operation.

        `kind` names a gate from list_gates; `spec` is that gate's parameters. You supply
        the claim and the test; a fixed gate returns the verdict. The hypothesis is
        recorded BEFORE evaluation, so a refused or rejected idea is still a fact on
        record and a later iteration will not re-derive it.
        """
        hypothesis = Hypothesis(
            question_id=question_id, statement=statement, kind=kind, spec=spec, rationale=rationale
        )
        if fingerprint(hypothesis) in self.ledger.tried_specs():
            return {
                "status": "duplicate",
                "message": "This exact test has already been run. Read get_progress() and "
                           "propose something genuinely different.",
            }
        self.ledger.append(hypothesis)
        evidence = gates.evaluate(hypothesis, self._gate_context(question_id))
        self.ledger.append(evidence)
        return {
            "status": "tested",
            "hypothesis_id": hypothesis.id,
            "evidence_id": evidence.id,
            "verdict": evidence.verdict.value,
            "effect_size": evidence.effect_size,
            "p_value": evidence.p_value,
            "sample_size": evidence.sample_size,
            "statistics": evidence.statistics,
            "warnings": evidence.warnings,
            "refusal_reason": evidence.refusal_reason,
            "guidance": _guidance(evidence),
        }

    def _gate_context(self, question_id: str) -> GateContext:
        question = self.ledger.by_id(question_id)
        target = None
        if isinstance(question, Question):
            acc = self.charter.accountability(question.accountability_id)
            if acc is not None and acc.metric in self.data.columns:
                target = self.data[acc.metric]
        return GateContext(
            data=self.data, charter=self.charter,
            standard=self.charter.evidence_standards, target=target,
        )

    # ---- write ----------------------------------------------------------------------
    def record_finding(self, evidence_id: str, headline: str, interpretation: str) -> dict[str, Any]:
        """Turn SUPPORTED evidence into an interpreted finding.

        Confidence is computed from the evidence, not accepted from you. A subgroup
        finding is refused until the same subgroup has been challenged with the
        driver_effect gate naming a plausible confound.
        """
        evidence = self.ledger.by_id(evidence_id)
        if not isinstance(evidence, Evidence):
            return {"status": "error", "message": f"no evidence {evidence_id!r}"}
        if evidence.verdict != Verdict.SUPPORTED:
            return {
                "status": "refused",
                "message": f"evidence {evidence_id} is {evidence.verdict.value}; only SUPPORTED "
                           f"evidence may become a finding",
            }
        if blocked := self._unchallenged(evidence):
            return blocked

        hypothesis = self.ledger.by_id(evidence.hypothesis_id)
        finding = Finding(
            hypothesis_id=evidence.hypothesis_id,
            evidence_id=evidence_id,
            question_id=getattr(hypothesis, "question_id", ""),
            headline=headline,
            interpretation=interpretation,
            confidence=confidence_from(evidence, self.charter.evidence_standards),
        )
        self.ledger.append(finding)
        return {
            "status": "recorded", "finding_id": finding.id, "confidence": finding.confidence,
            "note": "confidence is derived from the evidence, not self-assessed",
        }

    def _unchallenged(self, evidence: Evidence) -> dict[str, Any] | None:
        """Refuse a subgroup finding nobody has tried to break. Returns None if it is fine.

        Instructions are not a mechanism — a model that forgets, or reasons its way past
        them, produces a confident finding either way. This makes the adversarial step a
        precondition. The bar is that a confound was named and tested, not that the
        finding survived: a challenge that fails is as informative as one that succeeds.
        """
        if not self.charter.evidence_standards.requires_confound_control:
            return None
        hypothesis = self.ledger.by_id(evidence.hypothesis_id)
        where = (getattr(hypothesis, "spec", {}) or {}).get("where")
        if not where or evidence.gate == "driver_effect":
            return None

        challenges = [
            e for e in self.ledger.all("evidence")
            if e.gate == "driver_effect"
            and (getattr(self.ledger.by_id(e.hypothesis_id), "spec", {}) or {}).get("where") == where
        ]
        if not challenges:
            return {
                "status": "refused",
                "message": (
                    f"this subgroup has not been challenged. Before {evidence.id} can become a "
                    f"finding, run the driver_effect gate on the SAME where={where!r}, naming a "
                    f"plausible confound as `control` — an attribute that travels with this "
                    f"subgroup and could produce the result on its own (exposure, volume, tenure, "
                    f"mix, channel, size). An unchallenged subgroup difference is a correlation "
                    f"with a p-value."
                ),
                "required_gate": "driver_effect",
                "required_where": where,
            }
        worst = max((c.statistics.get("confound_explains_fraction") or 0.0) for c in challenges)
        if worst >= 0.5:
            controls = [c.statistics.get("control") for c in challenges]
            return {
                "status": "refused",
                "message": (
                    f"the challenge succeeded: {controls} explains ~{worst:.0%} of this "
                    f"association. The subgroup is substantially a proxy, so acting on it would "
                    f"spend the intervention on the wrong thing. Record a finding about the "
                    f"confound instead."
                ),
            }
        return None

    def propose_action(
        self,
        finding_ids: list[str],
        action_type: str,
        recommendation: str,
        params: dict[str, Any] | None = None,
        blast_radius: dict[str, Any] | None = None,
        observation_plan: dict[str, Any] | None = None,
        expected_effect: str = "",
    ) -> dict[str, Any]:
        """Convert findings into a typed, permissioned action a person can carry out.

        `action_type` must be one of the charter's decision rights. `params.where` must be
        the pandas rule that names the group acted on — the same rule you tested — and it
        is screened and counted against the data. `blast_radius.max_per_run` must state
        how many of the charter's unit the action touches. `observation_plan` must name a
        `metric`. The authorisation reasoning is always returned, including on refusal, so
        a "no" can be fixed or accepted rather than retried blindly.
        """
        if refused := self._untargeted(params, blast_radius, action_type):
            return refused
        findings = [f for f in self.ledger.all("finding") if f.id in set(finding_ids)]
        evidence_by_id = {e.id: e for e in self.ledger.all("evidence")}
        auth = authorise_action(
            charter=self.charter, action_type=action_type, findings=findings,
            evidence_by_id=evidence_by_id, requested_blast_radius=blast_radius,
            observation_plan=observation_plan,
        )
        if not auth.granted:
            return {"status": "refused", "authorisation": auth.as_dict()}

        decision = Decision(
            finding_ids=[f.id for f in findings], recommendation=recommendation,
            expected_effect=expected_effect,
        )
        self.ledger.append(decision)
        right = self.charter.right(action_type)
        # The group is counted by code, from the rule, so the report states how many the
        # action reaches rather than however many the model estimated.
        params = {**(params or {}),
                  "target_rows": int(gates._mask(self.data, params["where"]).sum())}
        blast_radius = {**(blast_radius or {}), "unit": right.blast_radius.unit if right else "records"}
        action = Action(
            decision_id=decision.id, action_type=action_type, params=params,
            blast_radius=blast_radius, observation_plan=observation_plan or {},
        )
        apply_authorisation(action, auth, right.reversible if right else True)
        self.ledger.append(action)
        return {
            "status": action.status, "action_id": action.id, "decision_id": decision.id,
            "autonomy_level": action.autonomy_level.value, "target_rows": params["target_rows"],
            "authorisation": auth.as_dict(),
        }

    def _untargeted(self, params, blast_radius, action_type) -> dict[str, Any] | None:
        """Refuse an action nobody could carry out. Returns None if it is fine.

        A person acting on this needs to know WHO it applies to and HOW MANY. The rule is
        screened like a hypothesis (no leakage, forbidden or sensitive columns — an action
        may not target what a test may not use) and must select somebody in this data.
        """
        where = (params or {}).get("where")
        if not isinstance(where, str) or not where.strip():
            return {"status": "refused", "message": (
                "params.where is required: the pandas rule naming the group this action "
                "applies to, normally the same `where` the supporting finding tested.")}
        try:
            gates.screen(where, self.data, self.charter)
        except gates.ScreenError as exc:
            return {"status": "refused", "message": f"params.where: {exc}"}
        if not gates._mask(self.data, where).any():
            return {"status": "refused",
                    "message": f"params.where selects nobody in this data: {where}"}
        requested = (blast_radius or {}).get("max_per_run")
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            right = self.charter.right(action_type)
            unit = right.blast_radius.unit if right else "records"
            return {"status": "refused", "message": (
                f"blast_radius.max_per_run is required: how many {unit} this action "
                f"touches, as a positive integer.")}
        return None


def _guidance(evidence: Evidence) -> str:
    """What a result MEANS for the next move. Without this an INCONCLUSIVE reads as a
    failure and the agent abandons a direction it never actually tested."""
    if evidence.verdict == Verdict.REFUSED:
        return ("This was not tested. Fix the stated problem or drop this line entirely — "
                "re-submitting the same shape will be refused again.")
    if evidence.verdict == Verdict.INCONCLUSIVE:
        return ("Underpowered or degenerate — NOT disproven. Broaden the rule to capture more "
                "records, or test the same idea at a coarser grain.")
    if evidence.verdict == Verdict.REJECTED:
        mdl = evidence.statistics.get("min_detectable_lift")
        if mdl and evidence.effect_size and evidence.effect_size < mdl:
            return ("Rejected, but the sample could not have detected an effect this small. "
                    "Treat as unresolved rather than false.")
        return "Genuinely not supported. Change the driver, not the wording."
    return ("Supported. Before recording it, ask what would make this spurious — a confound, "
            "a proxy, or a subgroup doing all the work — and test that with driver_effect.")
