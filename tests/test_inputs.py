"""The session's `intake/` folder decides whether a model runs, with no model.

What these pin down:

1. A session that brought nothing runs the defaults, untouched — the CLI path.
2. A role and data with no acceptance yields the intake report and NO model call.
3. Answers are picks from the offered list; anything else is refused with a reason,
   and a remap without a ratifier is refused — nobody's name, no run.
4. An accepted pairing is written to the session and the next turn runs it directly.
5. The gate short-circuits the agent pipeline: `call_next` is never reached until settled.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pandas as pd
import pytest

from loop.charter import load_charter
from loop.inputs import Inputs, Settled, presets, settle
from loop.intake import NOTHING_MEASURES_IT
from loop.runner import IntakeGate, RunScope

SERVICE = pathlib.Path(__file__).resolve().parent.parent / "src" / "strong-loop"


def _defaults() -> Settled:
    charter = load_charter(SERVICE / "charters" / "claims_analyst.yaml")
    return Settled(charter, pd.read_csv(SERVICE / "data" / "claims.csv"), 6)


def _write(inputs: Inputs, name: str, payload) -> None:
    inputs.dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (inputs.dir / name).write_text(text)


def test_nothing_brought_runs_the_defaults(tmp_path):
    out = settle(Inputs(tmp_path), _defaults())
    assert out.status == "run"
    assert out.settled.charter.role == "claims_analyst" and out.source["charter"] == "default"
    assert not (tmp_path / "intake").exists(), "settling the defaults writes nothing"


def test_presets_are_offered_by_name_never_by_path(tmp_path):
    assert set(presets()["charters"]) >= {"claims_analyst", "portfolio_analyst"}
    inputs = Inputs(tmp_path)
    _write(inputs, "request.json", {"charter": "../../etc/passwd", "data": "claims"})
    out = settle(inputs, _defaults())
    assert out.status == "refused" and "unknown charters preset" in out.problems[0]


def test_matching_pairing_awaits_acceptance_with_a_clean_report(tmp_path):
    inputs = Inputs(tmp_path)
    _write(inputs, "request.json", {"charter": "portfolio_analyst", "data": "customers"})
    out = settle(inputs, _defaults())
    assert out.status == "awaiting"
    assert out.report.ok and not out.report.clarifications
    assert out.source == {"charter": "preset:portfolio_analyst", "data": "preset:customers"}


def test_mismatch_asks_and_refuses_answers_off_the_menu(tmp_path):
    inputs = Inputs(tmp_path)
    data = pd.read_csv(SERVICE / "data" / "claims.csv").rename(columns={"leakage_flag": "leakage_indicator"})
    inputs.dir.mkdir()
    data.to_csv(inputs.data_path, index=False)
    _write(inputs, "request.json", {"charter": "claims_analyst"})

    out = settle(inputs, _defaults())
    assert out.status == "awaiting"
    [ask] = out.report.clarifications
    assert "leakage_indicator" in ask.candidates and ask.candidates[-1] == NOTHING_MEASURES_IT

    _write(inputs, "accept.json", {"answers": {ask.accountability_id: "settled_amount_x"}, "ratified_by": "sunil"})
    out = settle(inputs, _defaults())
    assert out.status == "refused" and "not one of the offered candidates" in out.problems[0]
    assert not inputs.pairing_path.exists()

    _write(inputs, "accept.json", {"answers": {ask.accountability_id: "leakage_indicator"}})
    out = settle(inputs, _defaults())
    assert out.status == "refused" and "ratified_by" in out.problems[0], "a remap needs a name"

    _write(inputs, "accept.json", {"answers": {ask.accountability_id: "leakage_indicator"},
                                   "ratified_by": "sunil", "iterations": 2})
    out = settle(inputs, _defaults())
    assert out.status == "run"
    assert out.settled.charter.status == "SIGNED" and out.settled.charter.ratified_by == "sunil"
    assert out.settled.charter.accountability(ask.accountability_id).metric == "leakage_indicator"
    assert out.settled.max_iterations == 2
    pairing = json.loads(inputs.pairing_path.read_text())
    assert pairing["remapped"] == {ask.accountability_id: "leakage_indicator"}
    assert load_charter(inputs.signed_path).content_hash, "the signed charter re-loads and verifies"

    # Next turn: settled already, no questions, same charter.
    again = settle(inputs, _defaults())
    assert again.status == "run" and again.settled.charter.ratified_by == "sunil"


def test_opt_out_drops_the_accountability(tmp_path):
    inputs = Inputs(tmp_path)
    data = pd.read_csv(SERVICE / "data" / "claims.csv").rename(columns={"days_to_settle": "cycle_days"})
    inputs.dir.mkdir()
    data.to_csv(inputs.data_path, index=False)
    out = settle(inputs, _defaults())
    [ask] = out.report.clarifications
    _write(inputs, "accept.json", {"answers": {ask.accountability_id: NOTHING_MEASURES_IT}, "ratified_by": "sunil"})
    out = settle(inputs, _defaults())
    assert out.status == "run"
    assert out.settled.charter.accountability(ask.accountability_id) is None
    assert len(out.report.questions) == 2


def test_unreadable_upload_is_refused_not_raised(tmp_path):
    inputs = Inputs(tmp_path)
    _write(inputs, "charter.yaml", "role: [not a charter")
    out = settle(inputs, _defaults())
    assert out.status == "refused" and out.problems[0].startswith("charter:")


# --- the middleware ------------------------------------------------------------------


class FakeSession:
    session_id = "s1"


class FakeContext:
    def __init__(self):
        self.session = FakeSession()
        self.stream = True
        self.result = None


def _updates(stream) -> list:
    async def drain():
        return [u async for u in stream]
    return asyncio.run(drain())


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOP_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    d = _defaults()
    scope = RunScope(d.charter, d.data, tmp_path / "runs", max_iterations=6)
    return IntakeGate(scope), scope


def test_gate_passes_the_defaults_through(gate):
    g, scope = gate
    ctx, called = FakeContext(), []

    async def call_next():
        called.append(True)

    asyncio.run(g.process(ctx, call_next))
    assert called and ctx.result is None
    run = scope.state_for(ctx.session, new_turn=True)
    assert run.charter.role == "claims_analyst" and run.max_iterations == 6


def test_gate_answers_with_the_report_and_never_calls_the_model(gate, tmp_path):
    g, scope = gate
    inputs = Inputs(tmp_path / "sessions" / "local")   # no hosted session id locally
    _write(inputs, "request.json", {"charter": "portfolio_analyst", "data": "customers"})
    ctx = FakeContext()

    async def call_next():
        raise AssertionError("the model must not run before the pairing is accepted")

    asyncio.run(g.process(ctx, call_next))
    updates = _updates(ctx.result)
    names = [c.name for u in updates for c in u.contents if c.type == "function_call"]
    assert names == ["loop.intake"]
    [result] = [c for u in updates for c in u.contents if c.type == "function_result"]
    payload = json.loads(result.result)
    assert payload["status"] == "awaiting" and payload["report"]["role"] == "portfolio_analyst"
    assert (inputs.dir / "intake.log").exists()

    _write(inputs, "accept.json", {"answers": {}})
    called = []

    async def call_next_ok():
        called.append(True)

    asyncio.run(g.process(FakeContext(), call_next_ok))
    assert called
    run = scope.state_for(FakeSession(), new_turn=True)
    assert run.charter.role == "portfolio_analyst" and "repeat_purchase" in run.data.columns
