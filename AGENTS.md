# AGENTS.md

Operational guide for agents working on this repo. Short on purpose.

**Read first:** `PLAN.md` — the decisions and why, the deployment coordinates, the platform
gotchas already paid for, and the roadmap. `README.md` — how to run it. `docs/index.html` —
the loop on one wheel, replayed from a recorded run. When a roadmap step lands, update `PLAN.md` §3 and §6.

## Commands

```bash
uv venv --python 3.13 && uv pip install -r src/strong-loop/requirements.txt pytest
python -m pytest tests -q                                   # no model, no network

cd src/strong-loop
python -m loop models                                       # which client LOOP_MODEL resolves to, one-token probe
python -m loop check --charter charters/<c>.yaml --data data/<d>.csv     # pairing valid, gate + screens bite, no model
python -m loop questions --charter charters/<c>.yaml
python -m loop run --charter charters/<c>.yaml --data data/<d>.csv --iterations 3   # the loop; needs a model
python -m loop sign charters/<c>.yaml --by <you>            # ratify a charter's content
python -m loop render charters/<c>.yaml                     # the same charter as standard Markdown (or back)

cd ../..
azd ai agent run --no-client                                # the hosted agent, locally, on :8088
azd ai agent invoke --local "Focus on ..."                 # every turn is one run; history is not replayed
azd up && azd ai agent invoke "go"                          # deploy, then invoke the deployed agent

# page 1 ("How it works"), regenerated from a recorded run (every number on it comes from the run);
# keep one run in docs/runs — tests check docs/index.html was built from it
python -m loop run --charter charters/claims_analyst.yaml --data data/claims.csv --iterations 2 --run-dir ../../docs/runs
python ../../tools/make_showcase.py ../../docs/runs/<run>        # -> docs/index.html
python ../../tools/make_explainer.py --speech-endpoint https://<account>.cognitiveservices.azure.com   # -> docs/explainer.{mp4,webm,vtt,jpg}; rerun after the line above
```

## The two rules that must never be broken

1. **Only `loop/gates.py` may construct an `Evidence`.** `tests/test_invariant_evidence.py`
   fails the build otherwise. If you need "another way to record a result", you need a gate.
2. **Only `authorise_action` may approve an `Action`.** Same reasoning; `loop/tools.py` is
   the only caller.

## Before you commit

Both must pass. They are the definition of "still generic": two charters that share no
vocabulary, through the same code, with no `if role == ...` anywhere.

```bash
python -m loop check --charter charters/portfolio_analyst.yaml --data data/customers.csv
python -m loop check --charter charters/claims_analyst.yaml    --data data/claims.csv
```

## Where things go

| Adding... | Goes in |
|---|---|
| a new **shape of question** | a new gate in `loop/gates.py`, via `@register` |
| a new **role** | a YAML in `src/strong-loop/charters/` (or a standard-Markdown charter uploaded) — never code |
| a new **charter key** | the model in `loop/charter.py`, `parse`/`render` in `loop/charter_md.py`, and a case in `tests/charter_md_cases.json` (the page's JS parser runs the same cases) |
| a new **safety rule** | `screen()` in `loop/gates.py` |
| a new **data-driven screen** (something the DATA proves may not be tested: a copy of the metric, a constant, an alias) | a rule in `loop/derived.py` — never a list in a charter |
| a new **model** | nothing: set `LOOP_MODEL`. `claude-*` routes to the Anthropic API, else `/openai/v1` |
| a new **tool** that certifies or touches the data | a method on `Toolbelt` in `loop/tools.py`, added to `tools()` |
| a new **reference tool or skill** (safe for every role) | `toolbox.yaml` / `src/strong-loop/skills/`, then `azd ai toolbox create` — never `code_interpreter` |

If a change requires editing `loop/` to support a new *use case*, an abstraction is missing.

## The loop, in one paragraph

`AgentLoopMiddleware(fresh_context=True)` re-runs the agent from an empty context each
iteration. `RunScope` (a `ContextProvider`) injects the iteration header + ledger summary
as instructions and the run's bound tools before every run, and finalises once per turn
after the loop ends. Finalisation is NOT in `should_continue`: the middleware's iteration
cap fires *before* the predicate, so on the last permitted run the predicate never runs.
Convergence reads the ledger, never the model's text. `Ledger.summary()` is loaded into
every fresh context; keep it small.

## Where runs live

`RunScope._new_run` → `runs/<user_id>/<session_id>/<role>-<stamp>-<conversation>`, ids from
`azure.ai.agentserver.core.get_request_context()` (all-None locally → flat layout). Path parts
go through `_safe()`. A finished run is never resumed by a later turn (`state_for(new_turn=True)`).

## The stream is the UI contract

`LoopEventStream` (outermost agent middleware) interleaves `loop.*` items into the Responses
stream from `RunState.pending`, which `RunScope.trace()` fills. Add a loop-level event by
calling `trace()`; it reaches `trace.log` and the stream in one step. Tool calls are NOT
re-emitted — they are already native items. `docs/live.html` renders the stream on the wheel
(`docs/wheel.js`, shared with page 1: add a new event kind to `Wheel.adapter`, `apply` and
`narrate` together, or the replay and the live page will disagree).

## Conventions

- No domain vocabulary in `loop/` logic. Docstring examples are fine; an `if role ==` is not.
- Comments explain *why*, never *what*.
- Tests assert on **refusals** as hard as successes.
