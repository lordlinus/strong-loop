"""runner.py — the loop. Ralph, applied to analysis, on agent-framework's own primitives.

    Ralph (coding)                  Here (analysis)
    ---------------------------     -----------------------------------
    fresh session per iteration     AgentLoopMiddleware(fresh_context=True)
    PLAN.md on disk                 the ledger on disk
    tests as backpressure           the evidence gate as backpressure
    "pick the most important task"  "pick the most important open question"

Each iteration starts with an empty context. It is not a conversation. The agent re-reads
the ledger every time, which is what stops quality degrading as a run gets long — and is
why `Ledger.summary()` must stay small.

Three parts, each doing one job:

  RunScope        a ContextProvider. Owns one RunState per session: the run directory, the
                  ledger, the convergence counters. Before every agent run it injects the
                  iteration header + ledger summary as instructions and the run's bound
                  tools; once per turn, after the loop ends, it finalises. That last hook
                  is why finalisation does not live in `should_continue`: the middleware's
                  iteration cap fires BEFORE the predicate, so on the last permitted run
                  the predicate is never called.
  should_continue reads durable ledger state — never the model's text — and stops on a
                  yield plateau. The cap is the middleware's job, not this function's.
  finalise        corrects for multiple testing across the WHOLE run, then writes report.json.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from agent_framework import Agent, AgentLoopMiddleware, ContextProvider

from . import gates
from .charter import RoleCharter
from .ledger import Ledger
from .models import build_chat_client
from .questions import questions_from
from .tools import Toolbelt
from .types import Verdict

# Stop when this many consecutive rounds add no supported evidence. Grinding a question
# space that has stopped yielding is the main way an autonomous loop wastes budget.
PATIENCE = 4
# A round that tests nothing is different from a round that tests and fails: the first
# means the agent is out of ideas, the second means the search is working and has not hit
# anything yet. Only the first is real exhaustion.
IDLE_PATIENCE = 2
MIN_ITERATIONS = 3
DEFAULT_MAX_ITERATIONS = 6
DEFAULT_STEER = "Begin. Work the open questions."

SYSTEM = """\
You are an autonomous analyst operating a role, not assisting a human. Nobody will read
a chart you make or answer a question you ask. Your output is evidence-backed findings
and authorised actions.

HOW THIS WORKS
- Your role's accountabilities, permitted actions and constraints come from `get_charter`.
- What the data looks like, and today's level of each metric, comes from `get_data_profile`.
- The shapes of claim you can test, and each one's spec, come from `list_gates`.
- Your open questions come from `list_questions`.
- You test claims with `test_hypothesis`. A fixed statistical gate returns the verdict.
  You cannot influence it, argue with it, or skip it.
- Only SUPPORTED evidence may become a finding. Only findings may justify an action.

RULES THAT MATTER
1. Call `get_progress` FIRST. You have no memory of previous iterations; the ledger is
   your memory. Never re-test something already tried.
2. Commit to a hypothesis before you see its result. No retro-fitting the claim.
3. INCONCLUSIVE means underpowered, NOT false. Broaden the rule or coarsen the grain.
   REFUSED means you broke a constraint — read the reason and do not repeat the shape.
4. When something is SUPPORTED, your next move is to challenge it, not to record it: run
   `driver_effect` on the same subgroup naming a plausible confound as `control`.
   `record_finding` refuses an unchallenged subgroup.
5. Prefer a small number of load-bearing findings over many weak ones.
6. Say when the data cannot answer a question. That is a real result, not a failure.
7. A REFUSED or INCONCLUSIVE result ends that hypothesis, NOT your iteration. Reformulate
   and keep testing. Constraints rule out shapes of question, never whole questions.
8. Anything you compute in your head is a hunch until a gate has scored it.

THROUGHPUT
Test SEVERAL hypotheses per iteration — aim for three to six `test_hypothesis` calls
before you summarise. You have a fixed number of iterations and each one starts from an
empty context, so an iteration spent on a single refused test is an iteration wasted.

Keep your closing summary to a few lines. Nobody reads it; the ledger is the record.
"""


@dataclass
class RunState:
    run_dir: pathlib.Path
    ledger: Ledger
    max_iterations: int
    iteration: int = 0
    tested: int = 0
    supported: int = 0
    stagnant: int = 0
    idle: int = 0
    finished: bool = False
    log: list[str] = field(default_factory=list)


def iteration_header(state: RunState) -> str:
    """The whole per-iteration context besides the system message and the human steer.

    Deliberately terse. Everything here competes for the same context budget the agent
    needs in order to think.
    """
    return (
        f"ITERATION {state.iteration} of {state.max_iterations}.\n\n"
        f"You have no memory of earlier iterations. This is the run state:\n\n"
        f"{json.dumps(state.ledger.summary(), indent=2, default=str)}\n\n"
        f"Choose the highest-value open question that is not yet exhausted, and make "
        f"real progress on it. Do not repeat any test listed above."
    )


class RunScope(ContextProvider):
    """One RunState per session; the run's tools and iteration header on every agent run."""

    after_run_once_per_turn = True

    def __init__(
        self,
        charter: RoleCharter,
        data: pd.DataFrame,
        runs_dir: pathlib.Path,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        *,
        echo: bool = False,
    ):
        super().__init__(source_id="strong_loop_run")
        self.charter = charter
        self.data = data
        self.runs_dir = pathlib.Path(runs_dir)
        self.max_iterations = max_iterations
        self.echo = echo
        self._runs: dict[str, RunState] = {}

    def state_for(self, session: Any) -> RunState:
        key = str(getattr(session, "session_id", None) or "cli")
        state = self._runs.get(key)
        if state is None:
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_dir = self.runs_dir / f"{self.charter.role}-{stamp}-{key[:8]}"
            ledger = Ledger(run_dir)
            if not ledger.all("question"):
                for question in questions_from(self.charter):
                    ledger.append(question)
            state = self._runs[key] = RunState(run_dir, ledger, self.max_iterations)
            self._say(f"--- run: {run_dir}")
        return state

    def _say(self, line: str) -> None:
        if self.echo:
            print(line, flush=True)

    # ---- ContextProvider hooks ------------------------------------------------------
    async def before_run(self, *, agent, session, context, state) -> None:
        run = self.state_for(session)
        run.iteration += 1
        self._say(f"\n=== iteration {run.iteration}/{run.max_iterations} ===")
        context.extend_instructions(self.source_id, iteration_header(run))
        context.extend_tools(self.source_id, Toolbelt(self.charter, self.data, run.ledger).tools())

    async def after_run(self, *, agent, session, context, state) -> None:
        # Deferred to the end of the loop by `after_run_once_per_turn`. Fires on cap-stop,
        # convergence-stop and exception alike.
        run = self.state_for(session)
        if not run.finished:
            run.finished = True
            self._tally(run, run.iteration)   # the cap-stopped last iteration
            report = finalise(self.charter, run)
            self._say(f"--- report: {run.run_dir / 'report.json'} "
                      f"({report['hypotheses_tested']} tested, {len(report['findings'])} findings)")

    # ---- AgentLoopMiddleware callbacks ----------------------------------------------
    def should_continue(self, *, iteration: int, session=None, **_) -> tuple[bool, str]:
        run = self.state_for(session)
        tested, gained = self._tally(run, iteration)
        converged = iteration >= MIN_ITERATIONS and (
            run.idle >= IDLE_PATIENCE or run.stagnant >= PATIENCE
        )
        if converged:
            why = "nothing left to test" if run.idle >= IDLE_PATIENCE else "no new supported evidence"
            run.log[-1] += f" — converged: {why}"
        self._say(f"--- {run.log[-1]}")
        return (not converged), run.log[-1]

    def _tally(self, run: RunState, iteration: int) -> tuple[int, int]:
        """Score an iteration from the ledger and log one line for it. Idempotent per
        iteration, because the last permitted iteration is scored from `after_run` (the
        middleware's cap fires before `should_continue`) and must not be scored twice."""
        if len(run.log) >= iteration:
            return 0, 0
        evidence = run.ledger.all("evidence")
        tested = len(evidence) - run.tested
        supported = sum(1 for e in evidence if e.verdict == Verdict.SUPPORTED)
        gained = supported - run.supported
        run.tested, run.supported = len(evidence), supported
        run.stagnant = 0 if gained else run.stagnant + 1
        run.idle = 0 if tested else run.idle + 1
        run.log.append(f"iteration {iteration}: tested {tested}, newly supported {gained}")
        return tested, gained

    def next_message(self, *, iteration: int, **_) -> str:
        # The ledger summary arrives through `before_run` as instructions, so the nudge
        # itself carries nothing that would go stale.
        return "Next iteration. Re-read the run state in your instructions and continue."

    def record_feedback(self, *, feedback: str | None = None, **_) -> str | None:
        return feedback


def finalise(charter: RoleCharter, run: RunState) -> dict[str, Any]:
    """Correct for multiple testing across the WHOLE run, then report."""
    evidence = run.ledger.all("evidence")
    # Count what the correction CHANGED. Counting every REJECTED record that carries an
    # adjusted p-value — the obvious formula — reports every honest rejection as a
    # demotion, which is how an earlier version reported 34 demotions in a run with
    # zero findings.
    before = {e.id: e.verdict for e in evidence}
    gates.apply_multiple_testing(evidence, charter.evidence_standards)
    demoted = [e for e in evidence if e.verdict != before[e.id]]
    report = {
        "role": charter.role,
        "run_dir": str(run.run_dir),
        "status": "complete",
        "iterations_run": run.iteration,
        "hypotheses_tested": len(evidence),
        "verdicts": run.ledger.summary()["verdict_counts"],
        "findings": [
            {"headline": f.headline, "confidence": f.confidence} for f in run.ledger.all("finding")
        ],
        "actions": [
            {"type": a.action_type, "status": a.status, "autonomy": a.autonomy_level.value}
            for a in run.ledger.all("action")
        ],
        "demoted_by_multiple_testing": len(demoted),
        "iteration_log": list(run.log),
    }
    (run.run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def default_runs_dir() -> pathlib.Path:
    # Under Foundry hosting `$HOME` persists per session, so a report written there
    # survives the turn.
    return pathlib.Path(os.environ.get("LOOP_RUNS_DIR") or (pathlib.Path.home() / "runs"))


def build_agent(
    charter: RoleCharter,
    data: pd.DataFrame,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    runs_dir: pathlib.Path | None = None,
    model: str | None = None,
    echo: bool = False,
) -> tuple[Agent, RunScope]:
    scope = RunScope(charter, data, runs_dir or default_runs_dir(), max_iterations, echo=echo)
    agent = Agent(
        client=build_chat_client(model),
        name="strong-loop",
        instructions=SYSTEM,
        context_providers=[scope],
        middleware=[
            AgentLoopMiddleware(
                scope.should_continue,
                max_iterations=max_iterations,
                next_message=scope.next_message,
                record_feedback=scope.record_feedback,
                inject_progress=False,   # the ledger IS the progress; never inject the log too
                fresh_context=True,      # the Ralph property
                return_final_only=True,  # CLI only; hosting always streams
            )
        ],
    )
    return agent, scope


async def run_once(
    charter: RoleCharter,
    data: pd.DataFrame,
    *,
    max_iterations: int,
    runs_dir: pathlib.Path,
    model: str | None = None,
    steer: str = DEFAULT_STEER,
) -> dict[str, Any]:
    """One complete run from the command line: build, loop, return the report."""
    agent, scope = build_agent(
        charter, data, max_iterations=max_iterations, runs_dir=runs_dir, model=model, echo=True
    )
    session = agent.create_session()
    response = await agent.run(steer, session=session)
    print(str(response.text)[:1500], flush=True)
    run = scope.state_for(session)
    return json.loads((run.run_dir / "report.json").read_text())
