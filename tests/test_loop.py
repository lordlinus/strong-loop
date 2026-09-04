"""The loop's mechanics, with no model: the provider's hooks and the middleware callbacks
over a scripted ledger. What these pin down:

1. Convergence reads the LEDGER, never the model's text.
2. The report is written by `after_run`, so it exists even when `should_continue` is never
   called — which is exactly what happens on the last permitted iteration, because the
   middleware's cap fires before the predicate.
3. Every agent run gets the iteration header and the run's bound tools injected.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pandas as pd
import pytest

from loop.charter import load_charter
from loop.runner import IDLE_PATIENCE, MIN_ITERATIONS, PATIENCE, RunScope
from loop.types import Evidence, Hypothesis, Verdict

SERVICE = pathlib.Path(__file__).resolve().parent.parent / "src" / "strong-loop"


class FakeSession:
    def __init__(self, session_id: str = "s1"):
        self.session_id = session_id


class FakeContext:
    def __init__(self):
        self.instructions: list[str] = []
        self.tools: list = []

    def extend_instructions(self, source_id, instructions):
        self.instructions.append(instructions)

    def extend_tools(self, source_id, tools):
        self.tools.extend(tools)


@pytest.fixture
def scope(tmp_path) -> RunScope:
    charter = load_charter(SERVICE / "charters" / "claims_analyst.yaml")
    data = pd.read_csv(SERVICE / "data" / "claims.csv")
    return RunScope(charter, data, tmp_path, max_iterations=4)


def _record(scope: RunScope, session, verdict: Verdict, i: int) -> None:
    """What one `test_hypothesis` call leaves behind — the side effect, not the model text."""
    run = scope.state_for(session)
    h = run.ledger.append(Hypothesis(question_id="q", statement=f"claim {i}",
                                     kind="proportion_lift", spec={"where": f"x > {i}"}))
    run.ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift", verdict=verdict,
                               p_value=0.001 if verdict == Verdict.SUPPORTED else 0.6,
                               effect_size=1.5, sample_size=300))


def test_before_run_injects_header_and_tools(scope):
    session, ctx = FakeSession(), FakeContext()
    asyncio.run(scope.before_run(agent=None, session=session, context=ctx, state={}))
    assert "ITERATION 1 of 4" in ctx.instructions[0]
    assert {t.name for t in ctx.tools} >= {"test_hypothesis", "record_finding", "propose_action", "get_progress"}
    run = scope.state_for(session)
    assert len(run.ledger.all("question")) == 3, "the charter's accountabilities became questions"


def test_convergence_reads_the_ledger_not_the_text(scope):
    session = FakeSession()
    # Iterations that test and fail: the search is working, keep going past MIN_ITERATIONS.
    for i in range(1, MIN_ITERATIONS + 1):
        _record(scope, session, Verdict.REJECTED, i)
        go, line = scope.should_continue(iteration=i, session=session)
        assert go, line
    # Two iterations that test NOTHING: out of ideas, stop.
    for i in range(MIN_ITERATIONS + 1, MIN_ITERATIONS + 1 + IDLE_PATIENCE):
        go, line = scope.should_continue(iteration=i, session=session)
    assert not go and "nothing left to test" in line


def test_stagnation_stops_after_patience(scope):
    session = FakeSession()
    total = MIN_ITERATIONS + PATIENCE
    for i in range(1, total + 1):
        _record(scope, session, Verdict.REJECTED, i)   # always testing, never supported
        go, line = scope.should_continue(iteration=i, session=session)
    assert not go and "no new supported evidence" in line
    # ...and a supported result resets the count.
    _record(scope, session, Verdict.SUPPORTED, 99)
    go, _ = scope.should_continue(iteration=total + 1, session=session)
    assert go


def test_report_is_written_without_should_continue_ever_running(scope):
    """The cap-stop path: after_run alone must finalise."""
    session, ctx = FakeSession(), FakeContext()
    asyncio.run(scope.before_run(agent=None, session=session, context=ctx, state={}))
    _record(scope, session, Verdict.SUPPORTED, 1)
    asyncio.run(scope.after_run(agent=None, session=session, context=ctx, state={}))
    run = scope.state_for(session)
    report = json.loads((run.run_dir / "report.json").read_text())
    assert report["hypotheses_tested"] == 1 and report["iterations_run"] == 1
    # a second after_run (a retry, a second stream close) does not double-finalise
    asyncio.run(scope.after_run(agent=None, session=session, context=ctx, state={}))
    assert json.loads((run.run_dir / "report.json").read_text()) == report


def test_multiple_testing_is_applied_at_finalise(scope):
    session, ctx = FakeSession(), FakeContext()
    asyncio.run(scope.before_run(agent=None, session=session, context=ctx, state={}))
    run = scope.state_for(session)
    lucky = run.ledger.append(Hypothesis(question_id="q", statement="lucky", kind="proportion_lift", spec={"where": "x > 0"}))
    run.ledger.append(Evidence(hypothesis_id=lucky.id, gate="proportion_lift", verdict=Verdict.SUPPORTED,
                               p_value=0.009, effect_size=1.3, sample_size=300))
    for i in range(1, 40):
        _record(scope, session, Verdict.REJECTED, i)
    asyncio.run(scope.after_run(agent=None, session=session, context=ctx, state={}))
    report = json.loads((run.run_dir / "report.json").read_text())
    assert report["demoted_by_multiple_testing"] == 1, "one lucky p just under 0.01 among 39 nulls must not survive BH"


def test_sessions_do_not_share_a_ledger(scope):
    a, b = FakeSession("a"), FakeSession("b")
    _record(scope, a, Verdict.REJECTED, 1)
    assert scope.state_for(a).run_dir != scope.state_for(b).run_dir
    assert len(scope.state_for(b).ledger.all("evidence")) == 0


def test_toolbox_is_optional(monkeypatch):
    """No toolbox configured means no static tools and no Azure credential needed."""
    from loop.runner import build_toolbox

    monkeypatch.delenv("TOOLBOX_ENDPOINT", raising=False)
    monkeypatch.delenv("TOOLBOX_NAME", raising=False)
    assert build_toolbox() is None
    monkeypatch.setenv("TOOLBOX_ENDPOINT", "")
    assert build_toolbox() is None, "azd injects unset variables as empty strings"
