# strong-loop

A role charter and a dataset in; evidence-backed findings and authorised actions out.

The agent proposes hypotheses. A fixed statistical gate is the only thing that can turn
one into evidence. An append-only ledger on disk is the only memory between iterations,
and every iteration starts with an empty context. That is Ralph applied to analysis, with
the evidence gate as backpressure the agent cannot rewrite.

```
charter (what the role owns, may do, must not touch)  ─┐
                                                        ├─▶ loop ─▶ ledger.jsonl ─▶ report.json
dataset (a CSV whose columns the charter's metrics name) ┘      ▲          │
                                                                │          │
                                            agent proposes ─────┘   gate decides
```

## Run it

```bash
uv venv --python 3.13 && uv pip install -r src/strong-loop/requirements.txt pytest
python -m pytest tests -q

cd src/strong-loop
python -m loop check --charter charters/claims_analyst.yaml --data data/claims.csv
python -m loop run   --charter charters/claims_analyst.yaml --data data/claims.csv --iterations 3
```

Models come through the APIM gateway. On a laptop `loop/models.py` reads the gateway and
key from `~/.config/azure-apim/apim-ssattiraju-01.env` if present; otherwise copy
`.env.example` to `.env`. Swap models with one variable — `LOOP_MODEL=claude-sonnet-4-6`
routes to the Anthropic API, anything else to `/openai/v1`. `python -m loop models` proves
a model works before you spend a run on it.

## As a Foundry hosted agent

The repo follows the Foundry hosted-agent template: `azure.yaml` at the root, the service
under `src/strong-loop/`, which carries everything the container needs (code, charters,
data).

```bash
azd ai agent run --no-client                                  # serves on :8088
azd ai agent invoke --local --new-session "Focus on the highest-priority accountability."
azd up                                                        # deploy; tokens via APIM, no model deployment needed
azd ai agent invoke --new-session "go"
```

**Every turn is one run.** The message you send is the human steer, and it is what every
fresh-context iteration starts from. Conversation history is deliberately not fed to the
agent (`history_source="agent"` in `main.py`): the ledger is its memory, and replaying a
prior run's transcript into a fresh-context loop is both wasteful and, as testing showed,
rejected by the model. Iterations, charter and dataset come from
`LOOP_MAX_ITERATIONS`, `LOOP_CHARTER`, `LOOP_DATA`. Reports land under `~/runs/`, which
Foundry persists per session.

## The toolbox

A Foundry toolbox is attached to the agent over MCP. It is declared in `toolbox.yaml` and
created once per project:

```bash
for s in src/strong-loop/skills/*/; do azd ai skill create "$(basename $s)" --file "$s/SKILL.md" --force; done
azd ai toolbox create strong-loop-toolbox --from-file toolbox.yaml   # writes TOOLBOX_STRONG_LOOP_TOOLBOX_MCP_ENDPOINT to the azd env
```

It holds four method skills (choosing a gate, challenging a finding, profiling a dataset,
proposing an action) and a `web` search. The skills arrive as MCP resources and are
exposed through `FoundryToolbox.as_skills_provider()`, so the agent sees a `load_skill`
tool and loads a skill's body only when it needs it. Everything in the toolbox is context:
it can inform a hypothesis and can never settle one. The three certifying operations stay
in `loop/tools.py`, bound per run, and `code_interpreter` is deliberately absent because a
toolbox reaches every charter the agent serves.

The toolbox is optional. With `TOOLBOX_ENDPOINT` unset the agent runs on its bound tools
alone and needs no Azure credential, which is what keeps the CLI and the tests
self-contained. Locally, export the endpoint from the azd env; in the container,
`azure.yaml` passes it through and the managed identity authenticates.

## What is here, and what is not

| `src/strong-loop/loop/` | |
|---|---|
| `types.py` | the contracts: Question → Hypothesis → Evidence → Finding → Decision → Action |
| `gates.py` | screens (PII, leakage, forbidden columns, code injection), three gates, BH correction. **The only place an Evidence is made.** |
| `stats.py` | the tests, no scipy |
| `charter.py` | the role as a contract, plus `authorise_action`, the only door to an Action |
| `ledger.py` | append-only JSONL; `summary()` is what a fresh iteration reads |
| `questions.py` | one question per accountability |
| `tools.py` | eight bound operations; three of them certify |
| `models.py` | one env var picks the model |
| `runner.py` | `AgentLoopMiddleware(fresh_context=True)` + `RunScope` |

Deliberately absent: a shell, a sandbox, a filesystem policy, a workflow graph, a run
registry, a front door, fuzzy column matching. Each is a later step if a
run proves it is needed, and not before.

Loop mechanics adapted from the [ralph-playbook](https://github.com/ClaytonFarr/ralph-playbook).
