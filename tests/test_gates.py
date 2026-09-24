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


# ---------------------------------------------------------------------------- screens
class TestScreens:
    def test_blocks_identifier_columns(self, charter, data):
        with pytest.raises(ScreenError, match="sensitive"):
            screen("record_id == record_id", data, charter)

    def test_blocks_charter_forbidden_columns(self, charter, data):
        with pytest.raises(ScreenError, match="forbidden"):
            screen("secret > 0", data, charter)

    def test_blocks_target_leakage(self, charter, data):
        with pytest.raises(ScreenError, match="leaking"):
            screen("converted == 1", data, charter)

    def test_blocks_arbitrary_code_execution(self, charter, data):
        # The screen is a sandbox, not a linter. These are the shapes that turn
        # `df.eval` into a remote code execution primitive.
        for evil in ["__import__('os')", "tenure.__class__", "tenure[0] > 1",
                     "(lambda: 1)()", "print(1)"]:
            with pytest.raises(ScreenError):
                screen(evil, data, charter)

    def test_blocks_unknown_columns(self, charter, data):
        with pytest.raises(ScreenError, match="unknown"):
            screen("nonexistent > 1", data, charter)

    def test_allows_legitimate_expression(self, charter, data):
        assert screen("tenure > 5 and noise < 0", data, charter) == {"tenure", "noise"}

    def test_screens_are_independent_of_naming_convention(self):
        """A screen that only reads snake_case protects only snake_case data.

        Real warehouse exports arrive as PascalCase, and an identifier or a protected
        attribute that slips through here reaches a prompt. The charter is the wrong
        place to compensate: it would have to re-list the same columns per dataset.
        """
        for spelling in ["employee_number", "EmployeeNumber", "Employee Number",
                         "EMPLOYEE-NUMBER", "employeeNumber"]:
            assert gates.is_sensitive_column(spelling), spelling

        for protected in ["MaritalStatus", "marital_status", "Gender", "Sex",
                          "EthnicOrigin", "religion"]:
            assert gates.is_sensitive_column(protected), protected

    def test_screens_do_not_overblock_legitimate_columns(self):
        """The counter-test. A screen nobody can analyse around is also useless.

        `unisex` and `Essex` contain 'sex'; `trace_count` contains 'race'. Matching
        whole words rather than substrings is what keeps these analysable.
        """
        for benign in ["unisex", "Essex", "trace_count", "event_name", "display_name",
                       "MonthlyIncome", "NumCompaniesWorked", "EmployeeCount",
                       "geo_country", "error_code", "Age"]:
            assert not gates.is_sensitive_column(benign), benign


# ------------------------------------------------------------------------------ gates
class TestGates:
    def test_finds_a_real_effect(self, charter, data):
        h = Hypothesis(question_id="q", statement="long tenure converts more",
                       kind="proportion_lift", spec={"where": "tenure > 8"})
        e = gates.evaluate(h, ctx(charter, data))
        assert e.verdict == Verdict.SUPPORTED
        assert e.effect_size > 1.2

    def test_rejects_pure_noise(self, charter, data):
        h = Hypothesis(question_id="q", statement="noise predicts conversion",
                       kind="proportion_lift", spec={"where": "noise > 0"})
        e = gates.evaluate(h, ctx(charter, data))
        assert e.verdict in (Verdict.REJECTED, Verdict.INCONCLUSIVE)

    def test_screen_failure_becomes_refused_not_an_exception(self, charter, data):
        # A refused test must still be a recorded fact, or the agent repeats it forever.
        h = Hypothesis(question_id="q", statement="peek at the target",
                       kind="proportion_lift", spec={"where": "converted == 1"})
        e = gates.evaluate(h, ctx(charter, data))
        assert e.verdict == Verdict.REFUSED
        assert "leaking" in e.refusal_reason

    def test_unknown_gate_is_refused(self, charter, data):
        h = Hypothesis(question_id="q", statement="?", kind="telepathy", spec={})
        assert gates.evaluate(h, ctx(charter, data)).verdict == Verdict.REFUSED

    def test_degenerate_rule_is_inconclusive_not_supported(self, charter, data):
        h = Hypothesis(question_id="q", statement="everyone", kind="proportion_lift",
                       spec={"where": "tenure > -1"})
        assert gates.evaluate(h, ctx(charter, data)).verdict == Verdict.INCONCLUSIVE

    def test_small_sample_is_inconclusive_not_rejected(self, charter, data):
        # The distinction that keeps the agent from abandoning untested ground.
        h = Hypothesis(question_id="q", statement="tiny slice", kind="proportion_lift",
                       spec={"where": "tenure > 25"})
        e = gates.evaluate(h, ctx(charter, data))
        if e.sample_size < charter.evidence_standards.min_sample_size:
            assert e.verdict == Verdict.INCONCLUSIVE

    def test_non_binary_target_is_refused_by_proportion_gate(self, charter, data):
        # A question whose metric is the continuous `amount`.
        amount_ctx = GateContext(data=data, charter=charter,
                                 standard=charter.evidence_standards, target=data["amount"])
        h = Hypothesis(question_id="q", statement="wrong gate", kind="proportion_lift",
                       spec={"where": "tenure > 5"})
        e = gates.evaluate(h, amount_ctx)
        assert e.verdict == Verdict.REFUSED
        assert "mean_shift" in e.refusal_reason

    def test_mean_shift_handles_continuous_measures(self, charter, data):
        amount_ctx = GateContext(data=data, charter=charter,
                                 standard=charter.evidence_standards, target=data["amount"])
        h = Hypothesis(question_id="q", statement="amount differs", kind="mean_shift",
                       spec={"where": "tenure > 8", "measure": "amount"})
        e = gates.evaluate(h, amount_ctx)
        assert e.verdict in (Verdict.SUPPORTED, Verdict.REJECTED)
        assert "cohens_d" in e.statistics

    def test_the_outcome_is_the_questions_metric_never_a_substitute(self, charter, data):
        """Seen live: a continuous metric made the model pick some other binary column as
        the driver_effect target, then write findings about the metric it never tested."""
        for kind, spec in (
            ("proportion_lift", {"where": "tenure > 5", "target": "amount"}),
            ("driver_effect", {"where": "tenure > 5", "control": "noise", "target": "amount"}),
            ("mean_shift", {"where": "tenure > 5", "measure": "amount"}),
        ):
            e = gates.evaluate(Hypothesis(question_id="q", statement="x", kind=kind, spec=spec),
                               ctx(charter, data))
            assert e.verdict == Verdict.REFUSED, kind
            assert "question's metric 'converted'" in e.refusal_reason, kind

    def test_driver_effect_handles_a_continuous_metric(self, charter, data):
        """The challenge gate must be reachable for a share or an amount, or the
        precondition on findings can never be met for such a role."""
        rng = np.random.default_rng(3)
        d = data.copy()
        # tenure raises amount; `noise` is unrelated, so controlling for it explains nothing
        d["amount"] = d["amount"] + 400 * (d["tenure"] > 5) + rng.normal(0, 50, len(d))
        amount_ctx = GateContext(data=d, charter=charter,
                                 standard=charter.evidence_standards, target=d["amount"])
        h = Hypothesis(question_id="q", statement="tenure drives amount", kind="driver_effect",
                       spec={"where": "tenure > 5", "control": "noise"})
        e = gates.evaluate(h, amount_ctx)
        assert e.verdict == Verdict.SUPPORTED
        assert e.statistics["test"] == "stratified_welch"
        assert e.statistics["confound_explains_fraction"] < 0.2
        # And a control that IS the driver's proxy explains the association away.
        d["proxy"] = (d["tenure"] > 5).astype(int) + rng.integers(0, 2, len(d)) * 0
        h2 = Hypothesis(question_id="q", statement="tenure drives amount", kind="driver_effect",
                        spec={"where": "tenure > 5", "control": "proxy"})
        e2 = gates.evaluate(h2, GateContext(data=d, charter=charter,
                                            standard=charter.evidence_standards, target=d["amount"]))
        assert e2.verdict == Verdict.INCONCLUSIVE, "a perfect proxy leaves no stratum with both sides"

    def test_driver_effect_tolerates_a_missing_control_value(self, charter, data):
        """Seen live: one NaN in a numeric control crashed the continuous path with a
        float-vs-str comparison inside the stratum loop; three challenges were REFUSED."""
        d = data.copy()
        d.loc[d.index[:3], "noise"] = np.nan
        amount_ctx = GateContext(data=d, charter=charter,
                                 standard=charter.evidence_standards, target=d["amount"])
        e = gates.evaluate(Hypothesis(question_id="q", statement="x", kind="driver_effect",
                                      spec={"where": "tenure > 5", "control": "noise"}), amount_ctx)
        assert e.verdict != Verdict.REFUSED, e.refusal_reason
        assert e.statistics["usable_strata"] >= 2

    def test_a_rule_that_is_the_metrics_definition_is_refused_as_circular(self, charter, data):
        d = data.copy()
        d["converted_copy_score"] = d["converted"] * 7          # a relabelling nobody listed
        c = GateContext(data=d, charter=charter, standard=charter.evidence_standards,
                        target=d["converted"])
        h = Hypothesis(question_id="q", statement="tautology", kind="proportion_lift",
                       spec={"where": "converted_copy_score > 0"})
        e = gates.evaluate(h, c)
        assert e.verdict == Verdict.REFUSED
        assert "circular" in e.refusal_reason
        m = Hypothesis(question_id="q", statement="tautology", kind="mean_shift",
                       spec={"where": "converted_copy_score > 0", "measure": "converted"})
        assert "circular" in gates.evaluate(m, c).refusal_reason

    def test_derived_leaks_are_refused_only_for_their_own_metric(self, charter, data):
        from loop.derived import Derived
        derived = Derived(leaks={"tenure": {"converted": "planted: tenure defines converted"}},
                          constants={"noise": "constant"})
        c = GateContext(data=data, charter=charter, standard=charter.evidence_standards,
                        target=data["converted"], derived=derived)
        e = gates.evaluate(Hypothesis(question_id="q", statement="x", kind="proportion_lift",
                                      spec={"where": "tenure > 5"}), c)
        assert e.verdict == Verdict.REFUSED and "circular" in e.refusal_reason
        e = gates.evaluate(Hypothesis(question_id="q", statement="x", kind="proportion_lift",
                                      spec={"where": "noise > 0"}), c)
        assert e.verdict == Verdict.REFUSED and "excluded" in e.refusal_reason
        # The same column, for a question about a different metric, is fine.
        other = GateContext(data=data, charter=charter, standard=charter.evidence_standards,
                            target=data["amount"], derived=derived)
        e = gates.evaluate(Hypothesis(question_id="q", statement="x", kind="mean_shift",
                                      spec={"where": "tenure > 5", "measure": "amount"}), other)
        assert e.verdict != Verdict.REFUSED


class TestMultipleTesting:
    def test_fdr_demotes_findings_that_were_only_lucky(self, charter):
        # The realistic failure mode: an untiring agent runs many tests, p-values come out
        # roughly uniform (the null), and a couple land just under 0.05 by chance. Those
        # are exactly the ones that would otherwise be reported as discoveries.
        borderline = Evidence(hypothesis_id="h_lucky", gate="proportion_lift",
                              verdict=Verdict.SUPPORTED, p_value=0.04, effect_size=1.3,
                              sample_size=200)
        nulls = [
            Evidence(hypothesis_id=f"h{i}", gate="proportion_lift", verdict=Verdict.REJECTED,
                     p_value=p, effect_size=1.0, sample_size=200)
            for i, p in enumerate(np.linspace(0.06, 0.99, 39))
        ]
        gates.apply_multiple_testing([borderline, *nulls], charter.evidence_standards)
        assert borderline.verdict == Verdict.REJECTED
        assert borderline.adjusted_p_value > 0.05
        assert any("benjamini_hochberg" in w for w in borderline.warnings)

    def test_fdr_is_not_applied_when_the_charter_opts_out(self, charter):
        standard = EvidenceStandard(multiple_testing_correction="none")
        e = Evidence(hypothesis_id="h", gate="proportion_lift", verdict=Verdict.SUPPORTED,
                     p_value=0.04, effect_size=1.3, sample_size=200)
        gates.apply_multiple_testing([e], standard)
        assert e.verdict == Verdict.SUPPORTED

    def test_fdr_keeps_a_genuinely_strong_result(self, charter):
        evidences = [
            Evidence(hypothesis_id="h0", gate="proportion_lift", verdict=Verdict.SUPPORTED,
                     p_value=1e-9, effect_size=2.0, sample_size=500)
        ] + [
            Evidence(hypothesis_id=f"h{i}", gate="proportion_lift", verdict=Verdict.REJECTED,
                     p_value=0.8, effect_size=1.0, sample_size=200)
            for i in range(1, 20)
        ]
        gates.apply_multiple_testing(evidences, charter.evidence_standards)
        assert evidences[0].verdict == Verdict.SUPPORTED


