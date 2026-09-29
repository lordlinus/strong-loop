"""The persona brief and the intake questionnaire, with no model.

What these pin down:
1. Every iteration is told who it is — the brief is the first thing in its instructions —
   and the report opens with the same brief and says what each accountability got.
2. Each advisory question lands in exactly one typed place, its default is safe, and an
   answer that was not on the menu is refused.
3. A text metric is answered from a closed list of its own values, signed by name, and the
   answer survives into the next turn of the session.
4. A default pairing that cannot load refuses sessions that bring nothing; it does not
   take the agent down.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import numpy as np
import pandas as pd
import pytest

from loop import gates
from loop.brief import coverage, render, role_brief
from loop.charter import Accountability, RoleCharter, load_charter
from loop.derived import Derived, derive
from loop.inputs import Inputs, Settled, settle
from loop.intake import (ATTR_ALLOW, ATTR_EXCLUDE, LEAK_NO, LEAK_UNSURE, LEAK_YES, TERM_NONE,
                         WEAK_WITHHOLD, advisories_for, apply_advice, data_notes, pair, resolve,
                         settle_advice)
from loop.questions import questions_from
from loop.runner import RunScope, finalise
from loop.types import Evidence, Finding, Hypothesis, Verdict

SERVICE = pathlib.Path(__file__).resolve().parent.parent / "src" / "strong-loop"


def _claims():
    return load_charter(SERVICE / "charters" / "claims_analyst.yaml"), pd.read_csv(SERVICE / "data" / "claims.csv")


def _portfolio():
    return load_charter(SERVICE / "charters" / "portfolio_analyst.yaml"), pd.read_csv(SERVICE / "data" / "customers.csv")


class FakeSession:
    session_id = "s1"


class FakeContext:
    def __init__(self):
        self.instructions: list[str] = []
        self.tools: list = []

    def extend_instructions(self, source_id, text):
        self.instructions.append(text)

    def extend_tools(self, source_id, tools):
        self.tools.extend(tools)


# ---- the brief -----------------------------------------------------------------------

def test_brief_states_the_role_from_the_charter_and_this_data():
    charter, data = _claims()
    b = role_brief(charter, data, derive(data, charter))
    assert b["role"] == "claims_analyst" and b["mission"].startswith("Owns the cost")
    assert [o["id"] for o in b["objectives"]] == ["reduce_leakage", "control_severity", "speed_settlement"]
    assert b["objectives"][0]["goal"] == "lower leakage_flag" and b["objectives"][0]["today"].startswith("rate ")
    plays = {p["action_type"]: p for p in b["plays"]}
    assert plays["revise_reserve_guidance"]["signs_off"] == "chief_actuary"
    assert plays["revise_reserve_guidance"]["stricter_bar"]["min_sample_size"] == 500
    never = {x["column"] for x in b["never"]}
    assert {"assessor_name", "claim_id", "leakage_flag"} <= never

    text = render(b)
    assert text.startswith("YOUR ROLE: claims_analyst")
    assert text.index("[reduce_leakage]") < text.index("[control_severity]") < text.index("[speed_settlement]")
    assert "chief_actuary signs off" in text and "n ≥ 150, p ≤ 0.01" in text
    assert "NEVER IN A RULE:" in text and "claim_id" in text


def test_brief_says_what_is_not_covered_and_when_nothing_may_be_done(tmp_path):
    charter = RoleCharter(role="r", accountabilities=[
        Accountability(id="a", statement="own x", metric="x", direction="increase"),
        Accountability(id="b", statement="own y", metric="y", direction="decrease")])
    data = pd.DataFrame({"x": [0, 1] * 50, "y": [3] * 100, "z": range(100)})
    text = render(role_brief(charter, data, derive(data, charter)))
    assert "1. [a] raise x" in text
    assert "Not covered this run: b (y): constant" in text
    assert "no action is granted" in text


def test_every_iteration_starts_from_the_brief_and_the_report_opens_with_it(tmp_path):
    charter, data = _claims()
    scope = RunScope(charter, data, tmp_path, max_iterations=2)
    ctx = FakeContext()
    asyncio.run(scope.before_run(agent=None, session=FakeSession(), context=ctx, state={}))
    (text,) = ctx.instructions
    assert text.startswith("YOUR ROLE: claims_analyst") and "ITERATION 1 of 2" in text
    run = scope.state_for(FakeSession())
    assert (run.run_dir / "brief.md").read_text().startswith("YOUR ROLE: claims_analyst")

    q = run.ledger.all("question")[0]
    h = run.ledger.append(Hypothesis(question_id=q.id, statement="s", kind="proportion_lift", spec={"where": "x > 1"}))
    e = run.ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift", verdict=Verdict.SUPPORTED,
                                   p_value=0.0001, effect_size=2.0, sample_size=500))
    run.ledger.append(Finding(hypothesis_id=h.id, evidence_id=e.id, question_id=q.id, headline="h", interpretation="i"))
    report = finalise(charter, run)
    assert report["brief"]["role"] == "claims_analyst"
    cov = {c["id"]: c for c in report["coverage"]}
    assert cov[q.accountability_id]["findings"] == 1 and cov[q.accountability_id]["outcome"].startswith("1 finding")
    assert all(c["outcome"] == "not reached" for k, c in cov.items() if k != q.accountability_id)


def test_coverage_counts_what_survived_the_correction(tmp_path):
    charter, data = _claims()
    scope = RunScope(charter, data, tmp_path, max_iterations=2)
    asyncio.run(scope.before_run(agent=None, session=FakeSession(), context=FakeContext(), state={}))
    run = scope.state_for(FakeSession())
    q = run.ledger.all("question")[0]
    h = run.ledger.append(Hypothesis(question_id=q.id, statement="s", kind="proportion_lift", spec={"where": "x > 1"}))
    e = run.ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift", verdict=Verdict.REJECTED,
                                   p_value=0.4, effect_size=1.0, sample_size=500))
    (row,) = [c for c in coverage(charter, run.ledger, [e]) if c["id"] == q.accountability_id]
    assert (row["tested"], row["supported"], row["outcome"]) == (1, 0, "tested; nothing cleared the evidence bar")


# ---- advisories ----------------------------------------------------------------------

def test_a_persons_attribute_is_asked_about_and_left_out_by_default():
    charter, data = _portfolio()
    derived = derive(data, charter)
    (ask,) = [a for a in advisories_for(charter, data, derived) if a.kind == "attribute"]
    assert (ask.id, ask.default) == ("attribute:age", ATTR_EXCLUDE)

    applied, ctx = settle_advice(charter, data, derived)
    assert ctx["advice"]["attribute:age"] == ATTR_EXCLUDE and "age" in applied.excluded()
    with pytest.raises(gates.ScreenError, match="excluded"):
        gates.screen("age > 40", data, charter, applied)
    assert "age" in {x["column"] for x in role_brief(charter, data, applied)["never"]}

    allowed, _ = settle_advice(charter, data, derived, {"attribute:age": ATTR_ALLOW})
    assert "age" not in allowed.excluded()
    gates.screen("age > 40", data, charter, allowed)   # no refusal


def test_an_attribute_the_charter_already_forbids_is_not_asked_about():
    charter = load_charter(SERVICE / "charters" / "hr_analyst.yaml")
    data = pd.read_csv(SERVICE / "data" / "hr_attrition.csv")
    assert "Age" in charter.constraints.forbidden_features
    assert not [a for a in advisories_for(charter, data, derive(data, charter)) if a.kind == "attribute"]


def _suspected() -> tuple[RoleCharter, pd.DataFrame, Derived]:
    charter = RoleCharter(role="r", accountabilities=[
        Accountability(id="a", statement="own m", metric="m", direction="increase")])
    data = pd.DataFrame({"m": np.arange(100) % 7, "c": np.arange(100) % 5, "k": np.arange(100) % 3})
    derived = Derived(suspected={"c": {"m": "nearly a monotone transform"}})
    return charter, data, derived


@pytest.mark.parametrize("answer,leaks,suspected", [
    (LEAK_YES, True, False), (LEAK_NO, False, False), (LEAK_UNSURE, False, True),
])
def test_a_suspected_leak_lands_where_the_answer_says(answer, leaks, suspected):
    charter, data, derived = _suspected()
    (ask,) = advisories_for(charter, data, derived)
    assert (ask.kind, ask.id, ask.default) == ("leak", "leak:c:m", LEAK_UNSURE)
    applied, _ = settle_advice(charter, data, derived, {ask.id: answer})
    assert bool(applied.leak_reason("c", "m")) is leaks
    assert bool(applied.suspect_reason("c", "m")) is suspected


def test_a_weak_metric_can_be_withheld_and_the_run_says_so():
    charter = RoleCharter(role="r", accountabilities=[
        Accountability(id="rare", statement="own m", metric="m", direction="increase"),
        Accountability(id="ok", statement="own n", metric="n", direction="increase")])
    data = pd.DataFrame({"m": [1] * 5 + [0] * 395, "n": [0, 1] * 200, "g": np.arange(400) % 4})
    derived = derive(data, charter)
    (ask,) = [a for a in advisories_for(charter, data, derived) if a.kind == "weak"]
    assert ask.id == "weak:rare"

    applied, _ = settle_advice(charter, data, derived, {"weak:rare": WEAK_WITHHOLD})
    qs = {q.accountability_id: q.status for q in questions_from(charter, applied)}
    assert qs == {"rare": "withheld", "ok": "open"}
    r = pair(charter, data, derived=applied)
    assert {a.id: a.status for a in r.accountabilities} == {"rare": "withheld", "ok": "ok"}
    assert r.ok


def test_a_glossary_term_is_bound_only_from_a_closed_list():
    charter, data = _portfolio()
    charter = charter.model_copy(update={"glossary": {"tenure": "Years as a customer.",
                                                      "wallet share": "Nothing in this data."}})
    derived = derive(data, charter)
    terms = {a.subject: a for a in advisories_for(charter, data, derived) if a.kind == "term"}
    assert list(terms) == ["tenure"]   # no candidate, no question
    assert terms["tenure"].candidates[0] == TERM_NONE and "tenure_years" in terms["tenure"].candidates

    _, ctx = settle_advice(charter, data, derived, {"term:tenure": "tenure_years"})
    assert ctx["bindings"] == {"tenure": "tenure_years"}
    assert 'IN THIS DATA, "tenure" is `tenure_years`.' in render(role_brief(charter, data, derived, ctx))
    _, ctx = settle_advice(charter, data, derived, {"term:tenure": "a column I made up"})
    assert ctx["bindings"] == {}   # off the menu is the default


def test_resolve_refuses_an_advisory_answer_off_the_menu():
    charter, data = _portfolio()
    r = pair(charter, data)
    res = resolve(charter, r, {"attribute:age": "maybe", "attribute:nope": ATTR_ALLOW})
    assert len(res.refused) == 2 and not res.advice
    res = resolve(charter, r, {"attribute:age": ATTR_ALLOW})
    assert res.advice == {"attribute:age": ATTR_ALLOW} and not res.changed and not res.refused


def test_snapshot_note_only_without_a_time_axis():
    no_time = pd.DataFrame({"contacts_last_year": [1, 2], "TrainingTimesLastYear": [0, 3]})
    assert data_notes(no_time, list(no_time.columns))
    assert not data_notes(pd.DataFrame({"signup_month": [1, 2]}), ["signup_month"])
    assert not data_notes(pd.DataFrame({"opened": ["2024-01-03", "2024-02-11"]}), ["opened"])


# ---- settle ----------------------------------------------------------------------------

def _write(inputs: Inputs, name: str, body) -> None:
    inputs.dir.mkdir(parents=True, exist_ok=True)
    (inputs.dir / name).write_text(body if isinstance(body, str) else json.dumps(body))


def _defaults() -> Settled:
    charter, data = _claims()
    return Settled(charter, data, 6)


def test_a_text_metric_is_encoded_signed_and_carried_into_the_next_turn(tmp_path):
    inputs = Inputs(tmp_path)
    _write(inputs, "charter.yaml", "role: r\naccountabilities:\n  - {id: a0, statement: own tier, metric: tier, direction: increase}\n")
    rng = np.random.default_rng(1)
    pd.DataFrame({"tier": rng.choice(["Gold", "Silver"], 400), "spend": rng.gamma(2, 3, 400),
                  "channel": rng.choice(["web", "agent"], 400)}).to_csv(inputs.dir / "data.csv", index=False)

    out = asyncio.run(settle(inputs, _defaults()))
    assert out.status == "awaiting"
    (ask,) = out.report.clarifications
    assert ask.kind == "encoding" and ask.id == "a0"

    _write(inputs, "accept.json", {"answers": {"a0": "Gold"}})
    out = asyncio.run(settle(inputs, _defaults()))
    assert out.status == "refused" and "ratified_by" in out.problems[0]   # a name goes on it

    _write(inputs, "accept.json", {"answers": {"a0": "Gold"}, "ratified_by": "sunil"})
    out = asyncio.run(settle(inputs, _defaults()))
    assert out.status == "run", out.problems
    assert set(out.settled.data["tier"].unique()) == {0, 1}
    assert json.loads(inputs.pairing_path.read_text())["encoded"] == {"tier": "Gold"}

    again = asyncio.run(settle(inputs, _defaults()))
    assert again.status == "run" and set(again.settled.data["tier"].unique()) == {0, 1}


def test_advisory_answers_are_recorded_and_reapplied(tmp_path):
    inputs = Inputs(tmp_path)
    _write(inputs, "request.json", {"charter": "portfolio_analyst", "data": "customers"})
    out = asyncio.run(settle(inputs, _defaults()))
    assert out.status == "awaiting"
    assert [a.id for a in out.report.advisories] == ["attribute:age"]
    assert out.report.brief["role"] == "portfolio_analyst"

    _write(inputs, "accept.json", {"answers": {"attribute:age": ATTR_ALLOW}})
    out = asyncio.run(settle(inputs, _defaults()))
    assert out.status == "run", out.problems   # an advisory needs no signature
    assert "age" not in out.settled.derived.excluded()
    assert json.loads(inputs.pairing_path.read_text())["advice"] == {"attribute:age": ATTR_ALLOW}

    again = asyncio.run(settle(inputs, _defaults()))
    assert "age" not in again.settled.derived.excluded()


def test_unanswered_advisories_take_their_default(tmp_path):
    inputs = Inputs(tmp_path)
    _write(inputs, "request.json", {"charter": "portfolio_analyst", "data": "customers"})
    _write(inputs, "accept.json", {"answers": {}})
    out = asyncio.run(settle(inputs, _defaults()))
    assert out.status == "run" and "age" in out.settled.derived.excluded()


# ---- fail soft -------------------------------------------------------------------------

def test_no_default_pairing_refuses_a_bare_session_and_serves_one_that_brings_its_own(tmp_path):
    broken = Settled(None, None, 6, problem="x.yaml: metric not in y.csv")
    out = asyncio.run(settle(Inputs(tmp_path / "bare"), broken))
    assert out.status == "refused" and "no default pairing" in out.problems[0] and "x.yaml" in out.problems[0]

    inputs = Inputs(tmp_path / "brings")
    _write(inputs, "request.json", {"charter": "claims_analyst", "data": "claims"})
    assert asyncio.run(settle(inputs, broken)).status == "awaiting"

    half = Inputs(tmp_path / "half")
    _write(half, "request.json", {"charter": "claims_analyst"})
    out = asyncio.run(settle(half, broken))
    assert out.status == "refused" and any(p.startswith("data: none uploaded") for p in out.problems)


def test_the_scope_reports_its_missing_default():
    scope = RunScope(None, None, pathlib.Path("/tmp/unused"), problem="bad default")
    d = scope.defaults()
    assert d.charter is None and d.problem == "bad default"
