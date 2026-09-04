# AGENTS.md

Operational guide for agents working on this repo. Short on purpose.

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

cd ../..
azd ai agent run --no-client                                # the hosted agent, locally, on :8088
azd ai agent invoke --local --new-session "Focus on ..."   # one NEW conversation = one run
azd up && azd ai agent invoke --new-session "go"            # deploy, then invoke the deployed agent
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
| a new **role** | a YAML in `src/strong-loop/charters/` — never code |
| a new **safety rule** | `screen()` in `loop/gates.py` |
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

## Conventions

- No domain vocabulary in `loop/` logic. Docstring examples are fine; an `if role ==` is not.
- Comments explain *why*, never *what*.
- Tests assert on **refusals** as hard as successes.
