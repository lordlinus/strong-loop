"""Loop events reach a streaming client as first-class items, in order, without a model.

The stream tap (`LoopEventStream`) sits outside the loop middleware and interleaves the
run's pending events with the model's updates: `iteration_start` before an iteration's
first update, `iteration_end` before the next nudge, `report` after the last update.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pandas as pd
from agent_framework import AgentResponseUpdate, Content, ResponseStream

from loop.charter import load_charter
from loop.runner import LoopEventStream, RunScope, loop_event_updates

SERVICE = pathlib.Path(__file__).resolve().parent.parent / "src" / "strong-loop"


class FakeSession:
    session_id = "s1"


class FakeContext:
    def __init__(self, inner):
        self.stream = True
        self.result = inner
        self.instructions: list[str] = []
        self.tools: list = []

    def extend_instructions(self, source_id, text):
        self.instructions.append(text)

    def extend_tools(self, source_id, tools):
        self.tools.extend(tools)


def _scope(tmp_path) -> RunScope:
    charter = load_charter(SERVICE / "charters" / "claims_analyst.yaml")
    data = pd.read_csv(SERVICE / "data" / "claims.csv")
    return RunScope(charter, data, tmp_path, max_iterations=2)


def _names(updates):
    out = []
    for u in updates:
        for c in u.contents:
            if c.type == "function_call":
                out.append(c.name)
            elif c.type == "text":
                out.append(f"text:{c.text}")
    return out


def test_pending_events_become_call_and_result_pairs(tmp_path):
    scope = _scope(tmp_path)
    session = FakeSession()
    asyncio.run(scope.before_run(agent=None, session=session, context=FakeContext(None), state={}))
    run = scope.state_for(session)
    updates = loop_event_updates(run)
    assert [c.type for u in updates for c in u.contents] == ["function_call", "function_result"]
    call, result = updates[0].contents[0], updates[1].contents[0]
    assert call.name == "loop.iteration_start" and call.call_id == result.call_id
    assert json.loads(result.result)["ledger_summary"]["questions_open"] == 3
    assert run.pending == [], "draining clears the queue"


def test_events_land_at_iteration_boundaries(tmp_path):
    scope = _scope(tmp_path)
    session = FakeSession()

    async def model_stream():
        # what the loop middleware would yield: iteration 1's text, a nudge, iteration 2's text
        await scope.before_run(agent=None, session=session, context=FakeContext(None), state={})
        yield AgentResponseUpdate(contents=[Content.from_text("thinking 1")], role="assistant")
        scope.should_continue(iteration=1, session=session)
        yield AgentResponseUpdate(contents=[Content.from_text("Next iteration.")], role="user")
        await scope.before_run(agent=None, session=session, context=FakeContext(None), state={})
        yield AgentResponseUpdate(contents=[Content.from_text("thinking 2")], role="assistant")
        await scope.after_run(agent=None, session=session, context=FakeContext(None), state={})

    ctx = FakeContext(ResponseStream(model_stream()))

    async def call_next():
        return None

    async def scenario():
        await LoopEventStream(scope).process(ctx, call_next)
        return [u async for u in ctx.result]

    names = _names(asyncio.run(scenario()))
    assert names == [
        "loop.iteration_start", "text:thinking 1",
        "loop.iteration_end", "text:Next iteration.",
        "loop.iteration_start", "text:thinking 2",
        "loop.iteration_end",   # the cap-stopped last iteration is tallied by after_run
        "loop.report",
    ], names


def test_non_streaming_runs_are_untouched(tmp_path):
    scope = _scope(tmp_path)
    ctx = FakeContext(None)
    ctx.stream = False
    sentinel = object()
    ctx.result = sentinel

    async def call_next():
        return None

    asyncio.run(LoopEventStream(scope).process(ctx, call_next))
    assert ctx.result is sentinel
