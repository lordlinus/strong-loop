"""
Tests for the invariants that make this a loop worth trusting.

These deliberately test the REFUSALS as hard as the successes. The value of this system
is not that it can find an effect — anything can find an effect. It is that it declines
to find ones that are not there, and declines to act on ones it is not allowed to act on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from loop import gates
from loop.charter import (
    Accountability,
    BlastRadius,
    Constraints,
    DecisionRight,
    EvidenceStandard,
    RoleCharter,
    authorise_action,
    validate_charter_shape,
)
from loop.gates import GateContext, ScreenError, screen
from loop.types import AutonomyLevel, Evidence, Finding, Hypothesis, Verdict


@pytest.fixture
def charter() -> RoleCharter:
    return RoleCharter(
        role="test_role",
        accountabilities=[
            Accountability(id="a1", statement="raise conversion", metric="converted",
                           direction="increase")
        ],
        decision_rights=[
            DecisionRight(
                action_type="flag", autonomy_level=AutonomyLevel.L2_ACT_REVERSIBLE,
                blast_radius=BlastRadius(unit="records", max_per_run=100),
                observation_metric="converted",
            ),
            DecisionRight(action_type="reprice", autonomy_level=AutonomyLevel.L1_RECOMMEND,
                          reversible=False),
        ],
        evidence_standards=EvidenceStandard(min_sample_size=50, max_p_value=0.05,
                                            min_effect_size=1.2),
        evidence_overrides={
            "reprice": EvidenceStandard(min_sample_size=50, requires_causal_design=True)
        },
        constraints=Constraints(leakage_features=["converted"], forbidden_features=["secret"]),
    )


@pytest.fixture
def data() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 2000
    tenure = rng.gamma(2, 3, n)
    # A real effect: tenure raises conversion.
    converted = rng.random(n) < 1 / (1 + np.exp(-(-2.0 + 0.2 * tenure)))
    return pd.DataFrame({
        "record_id": [f"R{i}" for i in range(n)],
        "tenure": tenure.round(2),
        "noise": rng.normal(0, 1, n),
        "secret": rng.normal(0, 1, n),
        "amount": rng.lognormal(6, 0.5, n).round(2),
        "converted": converted.astype(int),
    })


def ctx(charter, data, standard=None) -> GateContext:
    return GateContext(data=data, charter=charter,
                       standard=standard or charter.evidence_standards,
                       target=data["converted"])


# -------------------------------------------------------------------------- authority
class TestAuthority:
    def _supported(self) -> tuple[Finding, dict]:
        e = Evidence(hypothesis_id="h1", gate="proportion_lift", verdict=Verdict.SUPPORTED,
                     p_value=0.001, adjusted_p_value=0.004, effect_size=1.6, sample_size=400)
        f = Finding(hypothesis_id="h1", evidence_id=e.id, question_id="q",
                    headline="x", interpretation="y", confidence=0.8)
        return f, {e.id: e}

    def test_grants_a_well_founded_action(self, charter):
        f, ev = self._supported()
        auth = authorise_action(
            charter=charter, action_type="flag", findings=[f], evidence_by_id=ev,
            requested_blast_radius={"max_per_run": 50},
            observation_plan={"metric": "converted", "lag_days": 30},
        )
        assert auth.granted

    def test_refuses_an_action_type_the_charter_never_granted(self, charter):
        f, ev = self._supported()
        auth = authorise_action(charter=charter, action_type="delete_everything",
                                findings=[f], evidence_by_id=ev,
                                observation_plan={"metric": "converted"})
        assert not auth.granted
        assert "not in this role's decision rights" in auth.reasons[0]

    def test_refuses_when_there_is_no_way_to_observe_the_result(self, charter):
        # An action nobody ever measures cannot be learned from, so it cannot be taken.
        f, ev = self._supported()
        auth = authorise_action(charter=charter, action_type="flag", findings=[f],
                                evidence_by_id=ev, observation_plan=None)
        assert not auth.granted
        assert any("observation plan" in r for r in auth.reasons)

    def test_refuses_when_blast_radius_exceeds_the_cap(self, charter):
        f, ev = self._supported()
        auth = authorise_action(charter=charter, action_type="flag", findings=[f],
                                evidence_by_id=ev,
                                requested_blast_radius={"max_per_run": 5000},
                                observation_plan={"metric": "converted"})
        assert not auth.granted
        assert any("blast radius" in r for r in auth.reasons)

    def test_observational_evidence_cannot_satisfy_a_causal_requirement(self, charter):
        f, ev = self._supported()
        auth = authorise_action(charter=charter, action_type="reprice", findings=[f],
                                evidence_by_id=ev,
                                observation_plan={"metric": "converted"})
        assert not auth.granted
        assert any("causal design" in r for r in auth.reasons)

    def test_refuses_action_built_on_unsupported_evidence(self, charter):
        e = Evidence(hypothesis_id="h1", gate="proportion_lift", verdict=Verdict.REJECTED,
                     p_value=0.7, effect_size=1.0, sample_size=400)
        f = Finding(hypothesis_id="h1", evidence_id=e.id, question_id="q",
                    headline="x", interpretation="y")
        auth = authorise_action(charter=charter, action_type="flag", findings=[f],
                                evidence_by_id={e.id: e},
                                observation_plan={"metric": "converted"})
        assert not auth.granted


# ------------------------------------------------------------------ human-actionable
class TestActionsArePeopleSized:
    """The loop exists to make a person act. An action that names no group, or a group
    nobody can count, cannot be carried out, so `propose_action` refuses it before
    authority is even considered."""

    def _belt(self, charter, data, tmp_path):
        from loop.ledger import Ledger
        from loop.tools import Toolbelt

        belt = Toolbelt(charter, data, Ledger(tmp_path))
        h = belt.ledger.append(Hypothesis(question_id="q", statement="long tenure converts",
                                          kind="proportion_lift", spec={"where": "tenure > 5"}))
        e = belt.ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift",
                                        verdict=Verdict.SUPPORTED, p_value=0.001,
                                        adjusted_p_value=0.004, effect_size=1.6, sample_size=400))
        f = belt.ledger.append(Finding(hypothesis_id=h.id, evidence_id=e.id, question_id="q",
                                       headline="Tenure over 5 converts more",
                                       interpretation="y", confidence=0.8))
        return belt, f

    def _propose(self, belt, f, **over):
        kwargs = dict(finding_ids=[f.id], action_type="flag",
                      recommendation="Call every customer with tenure > 5 this month",
                      params={"where": "tenure > 5"}, blast_radius={"max_per_run": 60},
                      observation_plan={"metric": "converted", "lag_days": 30})
        kwargs.update(over)
        return belt.propose_action(**kwargs)

    def test_a_targeted_action_is_counted_by_code(self, charter, data, tmp_path):
        belt, f = self._belt(charter, data, tmp_path)
        res = self._propose(belt, f)
        assert res["status"] == "approved"
        assert res["target_rows"] == int((data["tenure"] > 5).sum()) > 0
        action = belt.ledger.all("action")[0]
        assert action.params["target_rows"] == res["target_rows"]
        assert action.blast_radius == {"max_per_run": 60, "unit": "records"}

    def test_refuses_an_action_with_no_group(self, charter, data, tmp_path):
        belt, f = self._belt(charter, data, tmp_path)
        res = self._propose(belt, f, params={"estimated": 400})
        assert res["status"] == "refused" and "params.where" in res["message"]
        assert not belt.ledger.all("action") and not belt.ledger.all("decision")

    def test_refuses_a_group_a_test_could_not_use(self, charter, data, tmp_path):
        # The same screen that guards hypotheses guards targets: no acting on the
        # target column, a forbidden column, or an identifier.
        belt, f = self._belt(charter, data, tmp_path)
        for where in ("converted == 1", "secret > 0", "record_id == 'R1'"):
            res = self._propose(belt, f, params={"where": where})
            assert res["status"] == "refused", where

    def test_refuses_a_group_that_selects_nobody(self, charter, data, tmp_path):
        belt, f = self._belt(charter, data, tmp_path)
        res = self._propose(belt, f, params={"where": "tenure > 10000"})
        assert res["status"] == "refused" and "selects nobody" in res["message"]

    def test_refuses_an_unstated_blast_radius(self, charter, data, tmp_path):
        # Without a stated count the charter's cap can never bite.
        belt, f = self._belt(charter, data, tmp_path)
        for br in (None, {}, {"count": 60}, {"max_per_run": 0}, {"max_per_run": "60"}):
            res = self._propose(belt, f, blast_radius=br)
            assert res["status"] == "refused" and "max_per_run" in res["message"], br

    def test_report_carries_what_a_person_needs(self, charter, data, tmp_path):
        from loop.runner import action_report

        belt, f = self._belt(charter, data, tmp_path)
        self._propose(belt, f)
        row = action_report(belt.ledger.all("action")[0], charter, belt.ledger)
        assert row["recommendation"].startswith("Call every customer")
        assert row["target"]["where"] == "tenure > 5" and row["target"]["rows"] > 0
        assert row["target"] == {"where": "tenure > 5", "rows": row["target"]["rows"],
                                 "unit": "records", "max_per_run": 60}
        assert row["observe"] == {"metric": "converted", "lag_days": 30}
        assert row["evidence"] == [{"headline": "Tenure over 5 converts more", "confidence": 0.8}]
        assert row["approval_from"] is None and row["status"] == "approved"


# ---------------------------------------------------------------------------- charter
class TestCharterShape:
    def test_rejects_a_charter_that_encodes_a_workflow(self):
        # The anti-leak rule: the moment a charter names screens or questions, the system
        # has stopped being a platform.
        for smell in ["questions", "workflow", "screens", "campaigns", "sql"]:
            problems = validate_charter_shape({"role": "r", smell: ["something"]})
            assert problems, f"{smell} should have been rejected"

    def test_accepts_a_clean_charter(self):
        assert validate_charter_shape(
            {"role": "r", "accountabilities": [], "decision_rights": []}
        ) == []

    def test_metric_must_be_an_identifier_not_prose(self):
        with pytest.raises(ValueError, match="single measure identifier"):
            Accountability(id="a", statement="be better",
                           metric="improve customer relationships", direction="increase")




class TestCharterSignature:
    def test_editing_a_signed_charter_is_detected(self, charter, tmp_path):
        """A recorded hash that is never checked is decoration, not authority."""
        import yaml
        from loop.charter import load_charter, sign, write_charter

        signed = sign(charter, "tests")
        clean = write_charter(signed, tmp_path / "clean.yaml")
        load_charter(clean)  # unmodified: loads

        # Granting a right nobody ratified is exactly what this must catch.
        raw = yaml.safe_load(clean.read_text())
        raw["decision_rights"].append({"action_type": "delete_everything"})
        tampered = tmp_path / "tampered.yaml"
        tampered.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError, match="modified since it was ratified"):
            load_charter(tampered)

        # …but re-signing is still possible, or the charter would be unfixable.
        load_charter(tampered, verify=False)


# ------------------------------------------------------------------ challenge precondition
class TestAChallengeMustHaveRun:
    """`record_finding` refuses a subgroup nobody tried to break. Seen live on the product
    adoption charter: the model's first two `driver_effect` calls were REFUSED (non-binary
    target) and still matched the subgroup's `where`, so they counted as challenges."""

    def _belt(self, charter, data, tmp_path):
        from loop.ledger import Ledger
        from loop.tools import Toolbelt

        belt = Toolbelt(charter, data, Ledger(tmp_path))
        h = belt.ledger.append(Hypothesis(question_id="q", statement="long tenure converts",
                                          kind="proportion_lift", spec={"where": "tenure > 5"}))
        e = belt.ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift",
                                        verdict=Verdict.SUPPORTED, p_value=0.001,
                                        effect_size=1.6, sample_size=400))
        return belt, e

    def _challenge(self, belt, verdict, **stats):
        h = belt.ledger.append(Hypothesis(question_id="q", statement="challenge",
                                          kind="driver_effect",
                                          spec={"where": "tenure > 5", "control": "amount"}))
        belt.ledger.append(Evidence(hypothesis_id=h.id, gate="driver_effect", verdict=verdict,
                                    statistics=stats, sample_size=400,
                                    refusal_reason="screen: needs a binary target"
                                    if verdict == Verdict.REFUSED else None))

    def test_a_refused_challenge_does_not_count(self, charter, data, tmp_path):
        belt, e = self._belt(charter, data, tmp_path)
        self._challenge(belt, Verdict.REFUSED)
        res = belt.record_finding(e.id, "Tenure over 5 converts more", "x")
        assert res["status"] == "refused"
        assert res["required_gate"] == "driver_effect"

    def test_an_inconclusive_challenge_does_not_count(self, charter, data, tmp_path):
        belt, e = self._belt(charter, data, tmp_path)
        self._challenge(belt, Verdict.INCONCLUSIVE, usable_strata=0)
        assert belt.record_finding(e.id, "Tenure over 5 converts more", "x")["status"] == "refused"

    def test_a_challenge_that_ran_and_failed_to_explain_it_lets_the_finding_through(
        self, charter, data, tmp_path
    ):
        belt, e = self._belt(charter, data, tmp_path)
        self._challenge(belt, Verdict.REJECTED, control="amount", confound_explains_fraction=0.1)
        assert belt.record_finding(e.id, "Tenure over 5 converts more", "x")["status"] == "recorded"

    def test_a_challenge_about_another_outcome_does_not_count(self, charter, data, tmp_path):
        """Seen live: a mean_shift finding on metric A was cleared by a driver_effect on the
        same `where` whose target was metric B."""
        from loop.ledger import Ledger
        from loop.tools import Toolbelt

        belt = Toolbelt(charter, data, Ledger(tmp_path))
        h0 = belt.ledger.append(Hypothesis(question_id="q", statement="long tenure converts",
                                           kind="proportion_lift", spec={"where": "tenure > 5"}))
        e = belt.ledger.append(Evidence(hypothesis_id=h0.id, gate="proportion_lift",
                                        verdict=Verdict.SUPPORTED, p_value=0.001, effect_size=1.6,
                                        sample_size=400, statistics={"target": "converted"}))
        h = belt.ledger.append(Hypothesis(question_id="q2", statement="challenge on B",
                                          kind="driver_effect",
                                          spec={"where": "tenure > 5", "control": "amount"}))
        belt.ledger.append(Evidence(hypothesis_id=h.id, gate="driver_effect", verdict=Verdict.SUPPORTED,
                                    statistics={"target": "other_metric", "control": "amount",
                                                "confound_explains_fraction": 0.1}, sample_size=400))
        assert belt.record_finding(e.id, "Tenure over 5 converts more", "x")["status"] == "refused"

    def test_a_driver_effect_that_calls_itself_a_proxy_cannot_be_recorded_directly(
        self, charter, data, tmp_path
    ):
        """Seen live: the model skipped the refused mean_shift and recorded the driver_effect
        evidence itself, whose own statistics said 82% of the association was the control."""
        from loop.ledger import Ledger
        from loop.tools import Toolbelt

        belt = Toolbelt(charter, data, Ledger(tmp_path))
        h = belt.ledger.append(Hypothesis(question_id="q", statement="x", kind="driver_effect",
                                          spec={"where": "tenure > 5", "control": "amount"}))
        e = belt.ledger.append(Evidence(hypothesis_id=h.id, gate="driver_effect", verdict=Verdict.SUPPORTED,
                                        p_value=0.001, effect_size=1.6, sample_size=400,
                                        statistics={"target": "converted", "control": "amount",
                                                    "confound_explains_fraction": 0.82}))
        res = belt.record_finding(e.id, "Tenure drives conversion beyond amount", "x")
        assert res["status"] == "refused" and "['amount']" in res["message"]
        # ...while a DIFFERENT subgroup whose control explained little is recordable on its
        # own. (The same subgroup is not: a successful challenge stays on its record.)
        h2 = belt.ledger.append(Hypothesis(question_id="q", statement="y", kind="driver_effect",
                                           spec={"where": "tenure > 8", "control": "amount"}))
        e2 = belt.ledger.append(Evidence(hypothesis_id=h2.id, gate="driver_effect", verdict=Verdict.SUPPORTED,
                                         p_value=0.001, effect_size=1.6, sample_size=400,
                                         statistics={"target": "converted", "control": "amount",
                                                     "confound_explains_fraction": 0.1}))
        assert belt.record_finding(e2.id, "Tenure drives conversion beyond amount", "x")["status"] == "recorded"

    def test_a_challenge_that_explains_the_subgroup_blocks_it_and_names_the_control(
        self, charter, data, tmp_path
    ):
        belt, e = self._belt(charter, data, tmp_path)
        self._challenge(belt, Verdict.REFUSED)   # must not appear as `None` in the message
        self._challenge(belt, Verdict.SUPPORTED, control="amount", confound_explains_fraction=0.8)
        res = belt.record_finding(e.id, "Tenure over 5 converts more", "x")
        assert res["status"] == "refused"
        assert "['amount']" in res["message"] and "None" not in res["message"]



# ------------------------------------------------------------------ same-rows dedupe
class TestSameRowsAreOneExperiment:
    """Seen live: `Acquired == 1` and `pillars_met >= 2` selected the same 96 customers and
    were both tested, recorded, and used as separate evidence."""

    def test_a_differently_spelled_rule_over_the_same_rows_is_a_duplicate(self, charter, data, tmp_path):
        from loop.ledger import Ledger
        from loop.tools import Toolbelt
        from loop.types import Question

        d = data.assign(senior=(data["tenure"] > 5).astype(int))
        belt = Toolbelt(charter, d, Ledger(tmp_path))
        q = belt.ledger.append(Question(accountability_id="a1", text="q", target_measure="converted"))
        first = belt.test_hypothesis(q.id, "long tenure converts", "proportion_lift", {"where": "tenure > 5"})
        assert first["status"] == "tested"
        second = belt.test_hypothesis(q.id, "seniors convert", "proportion_lift", {"where": "senior == 1"})
        assert second["status"] == "duplicate"
        assert second["same_as"] == first["hypothesis_id"]
        assert "tenure > 5" in second["message"]
        # A different gate over the same rows is a different experiment.
        third = belt.test_hypothesis(q.id, "seniors pay more", "mean_shift",
                                     {"where": "senior == 1", "measure": "converted"})
        assert third["status"] == "tested"
