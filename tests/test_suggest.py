"""suggest.py: TypeSafe proposes from a closed list, it never applies.

`_ask` is monkeypatched so these tests exercise probability ranking, the none option,
and closed-list validation without a real API call.
"""

from __future__ import annotations

import asyncio
import pandas as pd
import pytest

from loop.charter import Accountability, Constraints, RoleCharter
from loop.intake import NOTHING_MEASURES_IT, pair
from loop.suggest import suggest_mappings, unresolved


def charter_for(*metrics: str) -> RoleCharter:
    return RoleCharter(
        role="r",
        accountabilities=[
            Accountability(id=f"a{i}", statement=f"own {m}", metric=m, direction="increase")
            for i, m in enumerate(metrics)
        ],
        constraints=Constraints(),
    )


@pytest.fixture
def data() -> pd.DataFrame:
    return pd.DataFrame({
        "customer_id": [f"C{i}" for i in range(20)],
        "pillars_met": list(range(20)),
        "channel": ["web", "agent"] * 10,
    })


def test_unresolved_skips_metrics_with_a_lexical_clarification_already(data):
    charter = charter_for("attrition_flag")  # not present, no plausible column either
    r = pair(charter, data.assign(attrition_flag=0))
    assert unresolved(r) == []                 # present -> "ok", nothing to propose


def test_unresolved_lists_missing_metrics_with_no_lexical_candidates(data):
    charter = charter_for("capability_coverage_pct")
    r = pair(charter, data)
    assert not r.clarifications                # no shared subject -> pair() asked nothing
    assert unresolved(r) == ["a0"]


def test_unresolved_includes_missing_metrics_that_already_have_a_lexical_menu(data):
    """Seen live: a weak lexical menu silenced the review, and the plausible column was
    never offered. Every missing metric is reviewed; the menus are merged."""
    charter = charter_for("pillars_count")     # shares 'pillars' with pillars_met -> lexical menu
    r = pair(charter, data)
    assert r.clarifications and unresolved(r) == ["a0"]


def test_review_appends_typesafe_candidates_after_the_lexical_ones(data, monkeypatch):
    from loop.suggest import review

    async def fake(columns, accountabilities, model, glossary=None):
        return {"a0": {"choice": "channel", "confidence": 0.6, "exists": 0.8,
                       "probabilities": {"channel": 0.6, "pillars_met": 0.4}}}

    monkeypatch.setattr("loop.suggest._ask", fake)
    charter = charter_for("pillars_count")
    r = asyncio.run(review(charter, data, pair(charter, data)))
    (c,) = r.clarifications
    assert c.candidates[0] == "pillars_met", "the lexical match keeps first place"
    assert "channel" in c.candidates and c.candidates[-1] == NOTHING_MEASURES_IT
    assert c.candidates.count("pillars_met") == 1


def test_a_low_exists_answer_withholds_the_menu(data, monkeypatch):
    async def fake(columns, accountabilities, model, glossary=None):
        return {"a0": {"choice": "pillars_met", "confidence": 0.9, "exists": 0.1,
                       "probabilities": {"pillars_met": 0.9}}}

    monkeypatch.setattr("loop.suggest._ask", fake)
    charter = charter_for("capability_coverage_pct")
    assert asyncio.run(suggest_mappings(charter, data, pair(charter, data))) == []


async def _fake_ask_ok(columns, accountabilities, model, glossary=None):
    assert accountabilities[0]["id"] == "a0"
    assert "pillars_met" in columns
    assert "values" not in columns["channel"], "row values must not leave the service"
    return {
        "a0": {
            "choice": "pillars_met",
            "confidence": 0.82,
            "probabilities": {
                "pillars_met": 0.76,
                "not_a_real_column": 0.23,
                "none_of_the_above": 0.01,
            },
        }
    }


async def _fake_ask_junk(columns, accountabilities, model, glossary=None):
    return "not a response mapping"


def test_suggest_mappings_filters_to_real_columns(data, monkeypatch):
    monkeypatch.setattr("loop.suggest._ask", _fake_ask_ok)
    charter = charter_for("capability_coverage_pct")
    r = pair(charter, data)
    (c,) = asyncio.run(suggest_mappings(charter, data, r))
    assert c.accountability_id == "a0"
    assert c.candidates == ["pillars_met", NOTHING_MEASURES_IT]     # hallucination dropped
    assert "confidence 0.82" in c.question


def test_suggest_mappings_survives_a_non_json_reply(data, monkeypatch):
    monkeypatch.setattr("loop.suggest._ask", _fake_ask_junk)
    charter = charter_for("capability_coverage_pct")
    r = pair(charter, data)
    assert asyncio.run(suggest_mappings(charter, data, r)) == []


def test_suggest_mappings_accepts_typesafe_none(data, monkeypatch):
    async def none(columns, accountabilities, model, glossary=None):
        return {
            "a0": {
                "choice": "none_of_the_above",
                "confidence": 0.91,
                "probabilities": {"pillars_met": 0.09, "none_of_the_above": 0.91},
            }
        }

    monkeypatch.setattr("loop.suggest._ask", none)
    charter = charter_for("capability_coverage_pct")
    assert asyncio.run(suggest_mappings(charter, data, pair(charter, data))) == []


def test_suggest_mappings_is_a_noop_when_nothing_is_unresolved(data, monkeypatch):
    called = []

    async def fail_if_called(columns, accountabilities, model, glossary=None):
        called.append(True)
        return {}

    monkeypatch.setattr("loop.suggest._ask", fail_if_called)
    charter = charter_for("attrition_flag")
    r = pair(charter, data.assign(attrition_flag=0))
    assert asyncio.run(suggest_mappings(charter, data, r)) == []
    assert not called


# ------------------------------------------------------------------ semantic screens
def test_semantic_screens_route_by_probability_and_stay_schema_only(data, monkeypatch):
    from loop.derived import Derived
    from loop.suggest import semantic_screens, _SCREEN_CACHE
    _SCREEN_CACHE.clear()
    seen = {}

    async def fake(state, questions, model):
        seen["state"] = state
        return {"pillars_met|leaks|coverage": 0.9, "channel|leaks|coverage": 0.5,
                "customer_id|identifier": 0.95, "channel|identifier": 0.1,
                "not_a_column|leaks|coverage": 0.99}

    monkeypatch.setattr("loop.suggest._ask_nouls", fake)
    charter = charter_for("coverage")
    frame = data.assign(coverage=data["pillars_met"] / 4.0)
    out = asyncio.run(semantic_screens(charter, frame, Derived()))
    assert "coverage" in out.leaks["pillars_met"] and "P=0.90" in out.leaks["pillars_met"]["coverage"]
    assert "coverage" in out.suspected["channel"]
    assert "not_a_column" not in out.leaks, "answers are checked against the real column list"
    assert "customer_id" not in out.identifiers, "sensitive columns were never asked about"
    for col in seen["state"]["columns"].values():
        assert "values" not in col, "row values must not leave the service"


def test_semantic_screens_degrade_to_nothing_without_the_api(data, monkeypatch):
    from loop.derived import Derived
    from loop.suggest import semantic_screens, _SCREEN_CACHE
    _SCREEN_CACHE.clear()

    async def boom(state, questions, model):
        raise RuntimeError("no key")

    monkeypatch.setattr("loop.suggest._ask_nouls", boom)
    out = asyncio.run(semantic_screens(charter_for("pillars_met"), data, Derived()))
    assert out == Derived()


def test_semantic_screens_exclude_protected_proxies_and_warn_in_the_middle(data, monkeypatch):
    """The word list knows `gender`; it cannot know a senior-citizen flag is age."""
    from loop.derived import Derived
    from loop.suggest import semantic_screens, _SCREEN_CACHE
    _SCREEN_CACHE.clear()

    async def fake(state, questions, model):
        assert "pillars_met|protected" in questions
        return {"senior|protected": 0.9, "channel|protected": 0.5, "pillars_met|protected": 0.1}

    monkeypatch.setattr("loop.suggest._ask_nouls", fake)
    frame = data.assign(senior=[0, 1] * 10, coverage=data["pillars_met"] / 4.0)
    out = asyncio.run(semantic_screens(charter_for("coverage"), frame, Derived()))
    assert "senior" in out.excluded() and "P=0.90" in out.protected["senior"]
    assert "channel" not in out.excluded()
    assert any("'channel'" in w and "protected" in w for w in out.warnings)
    assert "pillars_met" not in out.protected

    # The code tier stays in charge of what it already decided, and a merged Derived keeps it.
    merged = Derived().merge(out)
    assert "senior" in merged.excluded()


def test_protected_screen_is_not_asked_when_the_charter_allows_sensitive_columns(data, monkeypatch):
    from loop.derived import Derived
    from loop.suggest import semantic_screens, _SCREEN_CACHE
    _SCREEN_CACHE.clear()
    asked = {}

    async def fake(state, questions, model):
        asked.update(questions)
        return {}

    monkeypatch.setattr("loop.suggest._ask_nouls", fake)
    charter = charter_for("coverage").model_copy(
        update={"constraints": Constraints(block_sensitive_columns=False)})
    asyncio.run(semantic_screens(charter, data.assign(coverage=data["pillars_met"] / 4.0), Derived()))
    assert asked and not any(k.endswith("|protected") for k in asked)


# ------------------------------------------------------------------ claim screens
class TestClaimScreens:
    """TypeSafe reads the model's prose against the ledger record it cites. It may refuse
    at high confidence or attach a warning; it never mints, and a dead API changes nothing."""

    @pytest.fixture
    def belt(self, tmp_path):
        from loop.charter import BlastRadius, DecisionRight, EvidenceStandard
        from loop.ledger import Ledger
        from loop.tools import Toolbelt
        from loop.types import AutonomyLevel, Evidence, Hypothesis, Question, Verdict

        charter = RoleCharter(
            role="r",
            accountabilities=[Accountability(id="a0", statement="raise conversion",
                                             metric="converted", direction="increase")],
            decision_rights=[DecisionRight(action_type="flag", description="flag a group",
                                           autonomy_level=AutonomyLevel.L1_RECOMMEND,
                                           blast_radius=BlastRadius(unit="customers", max_per_run=500))],
            evidence_standards=EvidenceStandard(min_sample_size=10, requires_confound_control=False),
        )
        frame = pd.DataFrame({"tenure": list(range(100)), "converted": [0, 1] * 50})
        belt = Toolbelt(charter, frame, Ledger(tmp_path))
        q = belt.ledger.append(Question(accountability_id="a0", text="q"))
        h = belt.ledger.append(Hypothesis(question_id=q.id, statement="long tenure converts",
                                          kind="proportion_lift", spec={"where": "tenure > 50"}))
        belt.e = belt.ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift",
                                             verdict=Verdict.SUPPORTED, p_value=0.001,
                                             effect_size=1.6, sample_size=49))
        return belt

    def _answers(self, monkeypatch, answers):
        seen = {}

        def fake(state, questions, model):
            seen.update(state=state, questions=questions)
            return answers

        monkeypatch.setattr("loop.suggest._ask_sync", fake)
        return seen

    def test_a_contradicting_headline_is_refused_and_nothing_recorded(self, belt, monkeypatch):
        seen = self._answers(monkeypatch, {"support": {"supports": 0.05, "contradicts": 0.9, "says_nothing": 0.05}})
        res = belt.record_finding(belt.e.id, "Long tenure converts LESS", "x")
        assert res["status"] == "refused" and "contradicts" in res["message"]
        assert belt.ledger.all("finding") == []
        assert seen["state"]["evidence"]["effect_size"] == 1.6
        assert seen["state"]["accountability"]["direction"] == "increase"

    def test_middle_band_records_with_warnings_the_report_carries(self, belt, monkeypatch):
        self._answers(monkeypatch, {"support": {"supports": 0.4, "contradicts": 0.3, "says_nothing": 0.3},
                                    "causal": 0.7})
        res = belt.record_finding(belt.e.id, "Tenure causes conversion", "x")
        assert res["status"] == "recorded" and len(res["warnings"]) == 2
        (f,) = belt.ledger.all("finding")
        assert any("causation" in w for w in f.warnings)

    def test_a_supported_claim_records_clean(self, belt, monkeypatch):
        self._answers(monkeypatch, {"support": {"supports": 0.95}, "causal": 0.1})
        res = belt.record_finding(belt.e.id, "Long tenure converts more", "x")
        assert res["status"] == "recorded" and "warnings" not in res

    def test_no_api_records_exactly_as_before(self, belt):
        res = belt.record_finding(belt.e.id, "Long tenure converts more", "x")
        assert res["status"] == "recorded" and belt.ledger.all("finding")[0].warnings == []

    def _finding(self, belt, monkeypatch):
        self._answers(monkeypatch, {"support": {"supports": 0.95}})
        return belt.record_finding(belt.e.id, "Long tenure converts more", "x")["finding_id"]

    def _propose(self, belt, fid, recommendation):
        return belt.propose_action(
            finding_ids=[fid], action_type="flag", recommendation=recommendation,
            params={"where": "tenure > 50"}, blast_radius={"max_per_run": 49},
            observation_plan={"metric": "converted"},
        )

    def test_a_study_dressed_as_an_action_is_refused(self, belt, monkeypatch):
        fid = self._finding(belt, monkeypatch)
        self._answers(monkeypatch, {"study": 0.92, "off_evidence": 0.1})
        res = self._propose(belt, fid, "Investigate why long-tenure customers convert")
        assert res["status"] == "refused" and "study" in res["message"]
        assert belt.ledger.all("decision") == [] and belt.ledger.all("action") == []

    def test_a_real_action_passes_and_middle_band_warns(self, belt, monkeypatch):
        fid = self._finding(belt, monkeypatch)
        self._answers(monkeypatch, {"study": 0.1, "off_evidence": 0.6})
        res = self._propose(belt, fid, "Flag customers with tenure over 50 for the upsell queue")
        assert res["status"] != "refused" and any("beyond" in w for w in res["warnings"])
        (d,) = belt.ledger.all("decision")
        assert d.warnings == res["warnings"]
