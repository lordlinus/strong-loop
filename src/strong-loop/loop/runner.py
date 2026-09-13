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

The toolbox, when configured, is the one static tool on the agent: a Foundry toolbox
reached over MCP, holding method skills and reference lookups. It is attached to the AGENT
rather than the run, so it holds only what is safe for every charter — context reach; it
can inform a hypothesis and can never settle one. See `toolbox.yaml` at the repo root.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from azure.ai.agentserver.core import get_request_context
from agent_framework import (
    Agent,
    AgentLoopMiddleware,
    AgentMiddleware,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    ContextProvider,
    ResponseStream,
    function_middleware,
)

from . import gates
from .charter import RoleCharter
from .inputs import Inputs, Outcome, Settled, session_home, settle
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
- If a toolbox is attached you also have method skills (load one with `load_skill` when
  its description fits what you are about to do) and a `web` search for reference.
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
8. Anything you compute in your head is a hunch until a gate has scored it. The same
   goes for anything the toolbox returns — a skill, a web result — it is context, never
   evidence.

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
    # What this run analyses. Settled per session by `IntakeGate`; the environment's
    # defaults when the session brought nothing of its own.
    charter: RoleCharter = None  # type: ignore[assignment]
    data: pd.DataFrame = None    # type: ignore[assignment]
    # Whose run this is, from the hosting platform's request headers. `None` locally.
    user_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    iteration: int = 0
    tested: int = 0
    supported: int = 0
    stagnant: int = 0
    idle: int = 0
    finished: bool = False
    log: list[str] = field(default_factory=list)
    # Loop-level events not yet surfaced in the response stream. `LoopEventStream` drains
    # this between model updates so a client sees iteration boundaries and the report as
    # first-class items, not as a stray line of text.
    pending: list[dict] = field(default_factory=list)


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
        self.runs_dir = pathlib.Path(runs_dir).resolve()
        self.max_iterations = max_iterations
        self.echo = echo
        self._runs: dict[str, RunState] = {}
        # Pairings `IntakeGate` has settled for a session, consumed by the next new run.
        self._settled: dict[str, Settled] = {}
        # The run most recently entered by `before_run`. The tool-call logger has no
        # session in hand, so it attributes calls to this run. Exact for one run at a
        # time (the CLI); best-effort under concurrent hosted conversations.
        self.current: RunState | None = None

    def state_for(self, session: Any, *, new_turn: bool = False) -> RunState:
        """The run for this conversation, creating it on first sight.

        `new_turn=True` (only `before_run` passes it) starts a fresh run when the previous
        one on this conversation has already finalised: with `history_source="agent"` a
        second turn in the same conversation is a second run, not a continuation.
        """
        key = self._key(session)
        state = self._runs.get(key)
        if state is None or (new_turn and state.finished):
            state = self._runs[key] = self._new_run(key)
        return state

    @staticmethod
    def _key(session: Any) -> str:
        return str(getattr(session, "session_id", None) or "cli")

    def defaults(self) -> Settled:
        return Settled(self.charter, self.data, self.max_iterations)

    def settle(self, session: Any, settled: Settled) -> None:
        """Bind the next run on this conversation to a settled pairing."""
        self._settled[self._key(session)] = settled

    def _new_run(self, conversation: str) -> RunState:
        """Lay out the run directory: runs/<user>/<hosted session>/<role>-<stamp>-<conv>.

        The two outer levels come from the platform's request headers (`x-agent-user-id`,
        the hosted session id), so one user's ledgers never sit beside another's and a
        session's runs can be listed in one place. Locally both are absent and the layout
        collapses to `runs/<role>-<stamp>-<conv>`, exactly as before.
        """
        ctx = get_request_context()
        user_id, session_id = ctx.user_id or None, ctx.session_id or None
        settled = self._settled.pop(conversation, None) or self.defaults()
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        parts = [_safe(x) for x in (user_id, session_id) if x]
        base = self.runs_dir.joinpath(*parts) / f"{settled.charter.role}-{stamp}-{_safe(conversation)[:8]}"
        run_dir, n = base, 1
        while run_dir.exists():  # two turns on one conversation within a second
            n += 1
            run_dir = base.with_name(f"{base.name}-{n}")
        ledger = Ledger(run_dir)
        if not ledger.all("question"):
            for question in questions_from(settled.charter):
                ledger.append(question)
        state = RunState(run_dir, ledger, min(settled.max_iterations, self.max_iterations),
                         charter=settled.charter, data=settled.data,
                         user_id=user_id, session_id=session_id, conversation_id=conversation)
        self._say(f"--- run: {run_dir}" + (f" (user {user_id})" if user_id else ""))
        return state

    def _say(self, line: str) -> None:
        if self.echo:
            print(line, flush=True)

    @staticmethod
    def trace(run: RunState, event: str, **fields: Any) -> None:
        """Append one JSON line to the run's `trace.log`.

        The ledger records what the platform BELIEVES; the trace records what HAPPENED —
        which tool was called, in which iteration, with what outcome. Kept apart so the
        ledger stays a typed record and the trace can carry anything. `docs/` is built
        from both.
        """
        entry = {"t": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 "iteration": run.iteration, "event": event, **fields}
        with (run.run_dir / "trace.log").open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        if event != "tool":  # tool calls and outputs are already native items in the stream
            run.pending.append(entry)

    # ---- ContextProvider hooks ------------------------------------------------------
    async def before_run(self, *, agent, session, context, state) -> None:
        run = self.state_for(session, new_turn=True)
        run.iteration += 1
        self.current = run
        self._say(f"\n=== iteration {run.iteration}/{run.max_iterations} ===")
        header = iteration_header(run)
        # The exact text this iteration was given, kept for the record and for `docs/`.
        (run.run_dir / "iterations").mkdir(exist_ok=True)
        (run.run_dir / "iterations" / f"{run.iteration}.md").write_text(header)
        self.trace(run, "iteration_start", max_iterations=run.max_iterations,
                   summary=run.ledger.summary(), user_id=run.user_id,
                   session_id=run.session_id, conversation_id=run.conversation_id)
        context.extend_instructions(self.source_id, header)
        context.extend_tools(self.source_id, Toolbelt(run.charter, run.data, run.ledger).tools())

    async def after_run(self, *, agent, session, context, state) -> None:
        # Deferred to the end of the loop by `after_run_once_per_turn`. Fires on cap-stop,
        # convergence-stop and exception alike.
        run = self.state_for(session)
        if not run.finished:
            run.finished = True
            self._tally(run, run.iteration)   # the cap-stopped last iteration
            report = finalise(run.charter, run)
            self.trace(run, "report", hypotheses_tested=report["hypotheses_tested"],
                       findings=len(report["findings"]), actions=len(report["actions"]),
                       demoted=report["demoted_by_multiple_testing"], report=report)
            self._say(f"--- report: {run.run_dir / 'report.json'} "
                      f"({report['hypotheses_tested']} tested, {len(report['findings'])} findings)")

    # ---- AgentLoopMiddleware callbacks ----------------------------------------------
    def should_continue(self, *, iteration: int, session=None, **_) -> tuple[bool, str]:
        run = self.state_for(session)
        tested, gained = self._tally(run, iteration)
        converged = iteration >= MIN_ITERATIONS and (
            run.idle >= IDLE_PATIENCE or run.stagnant >= PATIENCE
        )
        # The middleware's cap is the agent-wide ceiling and stays its job; a session may
        # have asked for FEWER, and only that smaller budget is enforced here.
        exhausted = run.max_iterations < self.max_iterations and iteration >= run.max_iterations
        if converged:
            why = "nothing left to test" if run.idle >= IDLE_PATIENCE else "no new supported evidence"
            run.log[-1] += f" — converged: {why}"
        elif exhausted:
            run.log[-1] += " — budget spent"
        self._say(f"--- {run.log[-1]}")
        return not (converged or exhausted), run.log[-1]

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
        self.trace(run, "iteration_end", tested=tested, newly_supported=gained,
                   stagnant=run.stagnant, idle=run.idle, line=run.log[-1])
        return tested, gained

    def next_message(self, *, iteration: int, **_) -> str:
        # The ledger summary arrives through `before_run` as instructions, so the nudge
        # itself carries nothing that would go stale.
        return "Next iteration. Re-read the run state in your instructions and continue."

    def record_feedback(self, *, feedback: str | None = None, **_) -> str | None:
        return feedback


def loop_event_updates(run: RunState) -> list[AgentResponseUpdate]:
    """Turn the run's pending loop events into stream updates, and clear them.

    Each event becomes a function call + result pair named `loop.<event>`. That is the one
    shape every Responses client already renders (as `function_call` /
    `function_call_output` items), so iteration boundaries, the ledger summary each
    iteration was given, and the final report arrive on the same channel as the tool
    calls — no side endpoint, no parsing of prose.
    """
    entries, run.pending[:] = list(run.pending), []
    return event_updates(entries, len(run.log))


def event_updates(entries: list[dict], tag: int = 0) -> list[AgentResponseUpdate]:
    """Loop events as stream updates. Split from `loop_event_updates` so an event that
    precedes any run — the intake verdict — travels the same way."""
    updates: list[AgentResponseUpdate] = []
    for entry in entries:
        event = entry["event"]
        call_id = f"loop_{event}_{entry['iteration']}_{tag}_{id(entry) & 0xffff:x}"
        args = {"iteration": entry["iteration"]}
        if event == "iteration_start":
            args["max_iterations"] = entry["max_iterations"]
            result: Any = {"ledger_summary": entry.get("summary", {})}
        elif event == "iteration_end":
            result = {k: entry[k] for k in ("tested", "newly_supported", "stagnant", "idle", "line") if k in entry}
        elif event == "report":
            result = entry.get("report", {})
        else:
            result = {k: v for k, v in entry.items() if k not in ("t", "iteration", "event")}
        updates.append(AgentResponseUpdate(
            contents=[Content.from_function_call(call_id=call_id, name=f"loop.{event}", arguments=args)],
            role="assistant", author_name="loop",
        ))
        updates.append(AgentResponseUpdate(
            contents=[Content.from_function_result(call_id=call_id, result=json.dumps(result, default=str))],
            role="tool", author_name="loop",
        ))
    return updates


class LoopEventStream(AgentMiddleware):
    """Outermost agent middleware: interleaves loop events into a streaming response.

    Sits OUTSIDE `AgentLoopMiddleware`, so it sees the whole run's stream. Before every
    model update it drains the run's pending events — `iteration_start` is queued by
    `before_run` before the iteration's first update exists, `iteration_end` by
    `should_continue` before the next nudge, and `report` by `after_run` after the last
    update — so each lands exactly at its boundary. Non-streaming runs are left alone:
    the ledger and `trace.log` already hold everything.
    """

    def __init__(self, scope: RunScope):
        self.scope = scope

    async def process(self, context, call_next) -> None:
        await call_next()
        if not getattr(context, "stream", False) or context.result is None:
            return
        inner = context.result
        scope = self.scope

        def pending() -> list[AgentResponseUpdate]:
            run = scope.current
            return loop_event_updates(run) if run is not None else []

        async def tapped():
            async for update in inner:
                for extra in pending():
                    yield extra
                yield update
            for extra in pending():
                yield extra

        context.result = ResponseStream(tapped(), finalizer=AgentResponse.from_updates)


class IntakeGate(AgentMiddleware):
    """Outermost of all: no model runs until the session's pairing is settled.

    Reads the session's `intake/` folder (see `loop.inputs`). Three outcomes:

      run       nothing was brought, or an accepted pairing exists — bind it to the next
                run and `call_next()`.
      awaiting  a role and data are present but nobody has accepted the pairing yet —
                answer with the intake report as a single `loop.intake` item and return
                WITHOUT calling the model. Zero tokens; the client renders the questions.
      refused   the inputs cannot be paired (bad preset, unreadable CSV, an answer that
                was not on the menu, no ratifier) — same shape, with the reasons.

    The event is written to `intake/intake.log` in the session rather than a run
    directory, because there is no run yet. Streaming only, like the rest of the loop's
    events: hosting always streams.
    """

    def __init__(self, scope: RunScope):
        self.scope = scope

    def outcome(self, session: Any) -> Outcome:
        ctx = get_request_context()
        inputs = Inputs(session_home(ctx.session_id or None))
        try:
            return settle(inputs, self.scope.defaults())
        except Exception as exc:  # a corrupt upload must never take the agent down
            return Outcome("refused", problems=[f"intake: {type(exc).__name__}: {exc}"])

    async def process(self, context, call_next) -> None:
        outcome = self.outcome(context.session)
        if outcome.status == "run":
            assert outcome.settled is not None
            self.scope.settle(context.session, outcome.settled)
            await call_next()
            return
        entry = {"t": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                 "iteration": 0, "event": "intake", **outcome.as_event()}
        self._log(entry)
        self.scope._say(f"--- intake: {outcome.status} {outcome.problems or ''}".rstrip())
        updates = event_updates([entry])
        if getattr(context, "stream", False):
            async def gen():
                for u in updates:
                    yield u
            context.result = ResponseStream(gen(), finalizer=AgentResponse.from_updates)
        else:
            context.result = AgentResponse.from_updates(updates)

    @staticmethod
    def _log(entry: dict) -> None:
        home = session_home(get_request_context().session_id or None)
        try:
            (home / "intake").mkdir(parents=True, exist_ok=True)
            with (home / "intake" / "intake.log").open("a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass


def _brief(value: Any, limit: int = 160) -> Any:
    """A tool argument or result, cut down to what a reader needs."""
    if isinstance(value, dict):
        keep = {k: v for k, v in value.items()
                if k in ("kind", "spec", "statement", "skill_name", "query", "action_type",
                         "evidence_id", "finding_ids", "status", "verdict", "effect_size",
                         "p_value", "sample_size", "refusal_reason", "hypothesis_id",
                         "finding_id", "action_id", "confidence", "message", "required_gate",
                         "authorisation", "autonomy_level", "guidance", "note")}
        return {k: (v if not isinstance(v, str) or len(v) <= limit else v[:limit] + "…")
                for k, v in keep.items()}
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _unwrap(result: Any) -> Any:
    """The framework hands middleware the tool's return value wrapped as `Content`
    items whose text is the serialised result. Recover the dict where there is one."""
    if isinstance(result, list) and result:
        result = result[0]
    for attr in ("result", "text"):
        inner = getattr(result, attr, None)
        if inner is not None:
            result = inner
            break
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result


def tool_logger(scope: RunScope):
    """Function middleware: one stdout line per tool call, and one trace line per call
    with the argument summary and the outcome. Observability only; the ledger is the
    record and this never influences a verdict."""

    @function_middleware
    async def log_tool_call(context, call_next) -> None:
        name = getattr(context.function, "name", "?")
        args = context.arguments
        if not isinstance(args, dict):
            args = getattr(args, "model_dump", lambda: {})() or {}
        head = str(args.get("kind") or args.get("skill_name") or args.get("query")
                   or args.get("action_type") or "")[:60]
        scope._say(f"    tool: {name} {head}".rstrip())
        await call_next()
        run = scope.current
        if run is not None:
            scope.trace(run, "tool", tool=name, args=_brief(args), result=_brief(_unwrap(context.result)))

    return log_tool_call


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
        "user_id": run.user_id,
        "session_id": run.session_id,
        "conversation_id": run.conversation_id,
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


def build_toolbox():
    """The Foundry toolbox as an MCP tool, or None when none is configured.

    `TOOLBOX_ENDPOINT` is what `azd ai toolbox create` writes to the azd environment
    (as `TOOLBOX_<NAME>_MCP_ENDPOINT`; azure.yaml maps it across). `TOOLBOX_NAME` plus the
    platform-injected `FOUNDRY_PROJECT_ENDPOINT` is the hosted fallback. Neither set means
    no toolbox, and the loop runs on its bound tools alone — the CLI must not need Azure
    credentials to work.
    """
    endpoint = os.environ.get("TOOLBOX_ENDPOINT") or None
    name = os.environ.get("TOOLBOX_NAME") or None
    if not (endpoint or name):
        return None
    from agent_framework_foundry_hosting import FoundryToolbox
    from azure.identity import DefaultAzureCredential

    return FoundryToolbox(
        DefaultAzureCredential(),
        url=endpoint,
        name=name or "toolbox",
        load_prompts=False,
        # The toolbox MCP endpoint is behind a preview feature flag.
        header_provider=lambda _: {"Foundry-Features": "Toolboxes=V1Preview"},
    )


def _safe(value: str) -> str:
    """A path component that cannot escape the runs directory or collide with a sibling."""
    return re.sub(r"[^A-Za-z0-9_.@-]", "-", str(value))[:96] or "unknown"


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
    toolbox = build_toolbox()
    providers = [scope]
    if toolbox is not None:
        # The toolbox serves its skills as MCP resources (`skill://index.json`), not as
        # tools; this provider advertises them and adds `load_skill`. Approval is off
        # because nobody is watching: an unattended loop that stops to ask may not load
        # a skill body is a loop that never loads one.
        providers.append(
            toolbox.as_skills_provider(
                disable_load_skill_approval=True,
                disable_read_skill_resource_approval=True,
            )
        )
    agent = Agent(
        client=build_chat_client(model),
        name="strong-loop",
        instructions=SYSTEM,
        tools=[toolbox] if toolbox is not None else None,
        context_providers=providers,
        middleware=[
            tool_logger(scope),
            IntakeGate(scope),           # first: no pairing, no model
            LoopEventStream(scope),      # sees the whole run's stream
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
    # Entering the agent connects (and on exit closes) the toolbox's MCP session, the
    # same way the hosting server does it.
    async with agent:
        response = await agent.run(steer, session=session)
    print(str(response.text)[:1500], flush=True)
    run = scope.state_for(session)
    return json.loads((run.run_dir / "report.json").read_text())
