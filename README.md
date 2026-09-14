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

Why it is built this way, what has been decided, and what comes next: `PLAN.md`.
How to work on it: `AGENTS.md`.

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
`LOOP_MAX_ITERATIONS`, `LOOP_CHARTER`, `LOOP_DATA`.

**Where a run's ledger lives.** Under `~/runs/` on the hosted session's filesystem, which
Foundry keeps across idle periods, laid out as
`runs/<user>/<hosted session>/<role>-<timestamp>-<conversation>/` with `ledger.jsonl`,
`trace.log`, `iterations/N.md` and `report.json` inside. The user and session come from
the platform's request headers (`x-agent-user-id` and the session id; `azd ai agent
invoke --user-identity <id>` sets the first), so one user's ledgers never sit beside
another's. Locally both are absent and the layout is just `runs/<role>-<timestamp>-…`.
The report carries the same three ids. A second turn on a conversation is a new run.

## Watch a run live

For local development, start both the agent and the live UI with:

```bash
make local-ui
```

Then open `http://localhost:8000/live.html`. The Make target serves the UI on port 8000,
starts the local agent on port 8088, and configures the page to use the local Responses
endpoint automatically.

Everything a run does goes out on the Responses stream, so any client can render it:
every tool call and its full output as `function_call` / `function_call_output` items,
the model's reasoning summaries and text, and the loop's own events as items named
`loop.iteration_start` (with the ledger summary that iteration was given),
`loop.iteration_end` (the tally `should_continue` read) and `loop.report` (the corrected
report). No side channel, no parsing of prose.

`docs/live.html` is a viewer for that stream. In production it asks the authenticated
control API for a short-lived stream ticket, then reads SSE directly from the streaming App
Service. For local development it calls `http://localhost:8088/responses` directly:

```bash
azd ai agent run --no-client          # or: cd src/strong-loop && python main.py
open docs/live.html
```

## Read the loop in detail

`docs/loop.html` walks the whole mechanism on one page, built from a recorded run in
`docs/runs/`: the charter as loaded, the data profile, the compiled questions, the exact
context each iteration received, every tool call joined to the ledger record it left, the
gate and screen source, the confound challenge, the authorisation checks and the corrected
report. Regenerate it with `tools/make_loop_doc.py` after any engine change; it has no
hand-written numbers.

`docs/showcase.html` is the same run on one non-scrolling screen: the loop as a ring around
the ledger, the model's moves outside it, code's verdicts inside it, the charter as input on
the left and report.json as output on the right. Press ▶ to replay the recorded trace through
the ring, or ● live to drive it from a running agent; click any station for the record behind
it. Built by `tools/make_showcase.py` from the same run directory.

## Customer site and deployment

The Static Web App preserves all three views as one customer journey:

1. `/showcase.html` — replay the outcome on one screen.
2. `/loop.html` — inspect the mechanism and its evidence trail.
3. `/live.html` — sign in with Microsoft Entra ID and steer a fresh hosted-agent run.

The live page never receives Azure credentials. Static Web Apps authentication protects the
page and linked control API, which creates sessions, uploads intake files, and issues a
60-second signed ticket. A second, unlinked App Service validates that ticket and relays the
Foundry SSE stream directly to the browser; this avoids buffering by the Static Web Apps API
proxy. Both services use the same managed identity. App Service is used because the
subscription's storage policy blocks Flex Consumption's OneDeploy path.

Bootstrap and configure the production pipeline once:

```bash
./scripts/provision-web.sh
./scripts/setup-azure-auth-for-pipeline.sh <github-owner/repository>
```

Then pushes to `master` run `.github/workflows/deploy.yml`: tests first, then independent
agent, API and site deployments behind the `production` GitHub environment. Pull requests
run tests only. Configure required reviewers on that environment if deployment approval is
required. The workflow uses GitHub OIDC for Azure; only the APIM key and Static Web Apps
deployment token are stored as environment secrets.

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
