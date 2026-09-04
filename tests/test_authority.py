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
