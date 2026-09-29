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
source .venv/bin/activate      # or `make venv && make test`, which uses .venv/bin/python directly
python -m pytest tests -q

cd src/strong-loop
python -m loop check --charter charters/claims_analyst.yaml --data data/claims.csv --brief
python -m loop run   --charter charters/claims_analyst.yaml --data data/claims.csv --iterations 3

# answer what `check` asked, from its closed lists (no free text, no derivation script).
# A metric carried under another name, or as text, is remapped or encoded and signed by
# name; an advisory question left unanswered takes the default `check` marks with *
python -m loop run --charter charters/<role>.yaml --data data/<export>.csv \
    --answer <accountability_id>=<column> --answer 'attribute:age=yes — this role may use it' \
    --ratified-by <you>

# write a charter as Markdown instead: every command above takes a .md charter too
python -m loop render charters/claims_analyst.yaml > my_role.md    # a worked example to edit
python -m loop check --charter my_role.md --data data/claims.csv
```

A Markdown charter has one standard shape — flat front matter (`role`, `version`,
`extends`), then fixed sections `## Glossary`, `## Accountabilities`, `## Decision rights`,
`## Evidence standard`, `## Constraints`, each a list of `- key: value` bullets under
`### <id> — <text>` items (the shape is documented at the top of `loop/charter_md.py`). It
is read by fixed rules, not a model, into the same charter as the YAML, so signing and every
screen work unchanged. An unknown heading or key is an error with its line number; lines
starting with `>` are commentary. Shipped charters stay YAML.

What may be tested is worked out from the data, per run, by `loop/derived.py` — not from a
list in the charter. A column that is a copy, rescaling, threshold or component of a metric
is refused for hypotheses about that metric and left alone for the others; constants and
duplicate columns are never offered; a metric that cannot move withholds its question and
the run proceeds. `check` prints all of it under `derived screens:`. Each gate additionally
refuses a rule that partitions its outcome exactly, and only ever tests the question's own
metric.

Models come through the APIM gateway. On a laptop `loop/models.py` reads the gateway and
key from `~/.config/azure-apim/apim-ssattiraju-01.env` if present; otherwise copy
`.env.example` to `.env`. Swap models with one variable — `LOOP_MODEL=claude-sonnet-4-6`
routes to the Anthropic API, anything else to `/openai/v1`. `python -m loop models` proves
a model works before you spend a run on it.

**Changing the deployed model** is a setting, not a code change: `make model MODEL=<name>`
(add `TYPESAFE_MODEL=<name>` for TypeSafe's). It probes the model through the gateway,
sets `LOOP_MODEL` on the GitHub `production` environment — the one place it lives for
deploys; it overrides any repo-level variable, and empty means the default in
`loop/models.py` — then runs the deploy workflow and waits. The workflow probes again
before `azd deploy` and fails if the deployed agent version does not carry that model, so
a name the gateway does not serve never reaches the live agent. `scripts/set-model.sh
--default` returns to the code default; `--no-deploy` leaves it for the next push.

TypeSafe is optional and serves three narrow purposes, none of which sends a row or sample
value. The frontier model does the reasoning — hypotheses, confounds, interpretation;
TypeSafe answers the small closed questions around it. When an uploaded charter metric has
no lexical match in an uploaded dataset, one batched `Choice` request ranks the
already-screened columns and may select `none_of_the_above`; a person still chooses and
ratifies the mapping. A second tier of the derived screens asks, from schema metadata, per
column and metric whether the NAMES say one is a copy, component or consequence of the
other, and per column whether it is an identifier or a protected attribute or proxy (a
senior-citizen flag the word list cannot know): P≥0.70 refuses, 0.35–0.70 warns, and the
data-driven tier always wins. And the model's finding and action prose is read against the
ledger record it cites: a headline that contradicts its evidence, or a "recommendation" that
is only a study, is refused at P≥0.80; a causal claim or a recommendation reaching beyond its
findings is recorded with a warning the report shows. Set `TYPESAFE_API_KEY` in
`src/strong-loop/.env`. TypeSafe never creates evidence, findings, or actions, and
everything falls back to the code-only behaviour if it is unconfigured or unavailable.

Yes/No-style text columns (`Yes`/`No`, `Y`/`N`, `True`/`False`, both answers present) and
numbers stored as text are read as numbers at load; `check` and the pairing report list
every column read this way. Anything more ambiguous stays text.

**Before a run, the platform asks what code cannot settle** — always as a closed list,
generated from the charter and the schema, never by a model. Two kinds block the run until
answered: which column measures a metric the data does not carry by that name, and, for a
metric that arrives as text with a handful of values (`Churned`/`Active`/`Paused`), which
value is the event. Four are advisory, each with a safe default that is applied and
recorded if nobody answers: a column suspected (not proven) of leaking a metric, a
person's attribute such as `age` that the screens do not block (default: leave it out), a
metric too rare for any subgroup to clear the gate (keep or withhold), and which column a
glossary term means in this data. Answers land in `intake/pairing.json` and are
re-applied on every later turn of the session.

**Then the agent is told who it is.** `loop/brief.py` builds one brief from the charter and
the pairing: the objectives in priority order with today's level, the plays with who acts
and who signs off, the columns it may never put in a rule, the proof the gate requires, and
what the data's shape means (no date column → associations only). The live page shows it
before start; every fresh iteration's instructions open with it (`check --brief` prints
it); the report opens with it and ends each accountability with what the run got
(`coverage`).

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

**Deploying it from a fresh machine.** `azd up` assumes a machine that is already set up
and an azd environment that already carries the gateway, the toolbox endpoint and the
agent's role grant. `scripts/deploy.sh` is the handover path that assumes none of it:

```bash
./scripts/deploy.sh --check-only   # tooling, sign-in, permissions, gateway — changes nothing
./scripts/deploy.sh                # the whole deployment
./scripts/deploy.sh --smoke        # ...and invoke the deployed agent once at the end
```

It checks `az`/`azd` versions and installs the three Foundry azd extensions, signs both
CLIs in, asks Azure (not a role name) whether the signed-in account holds the actions the
deployment needs, registers the resource providers, and proves the APIM gateway answers
for `LOOP_MODEL` before spending a deployment on it. Then it provisions the project,
creates Application Insights and connects it so traces appear, publishes the four skills
and the toolbox, deploys the agent, and grants the agent's managed identity **Foundry
User** on the account — the grant without which reading a skill body fails. Every step is
idempotent, and anything it cannot do itself (a missing role assignment permission, for
instance) it prints as the exact command for someone who can. `make deploy` and
`make deploy-check` are the same two entry points.

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

Then open `http://localhost:8000/live.html`. The Make target creates `.venv` with uv if it
is missing, serves the UI on port 8000, starts the local agent on port 8088, and configures
the page to use the local Responses endpoint automatically. Every target runs
`.venv/bin/python` explicitly — `make` uses `/bin/sh`, where a bare `python` is usually not
on PATH.

Everything a run does goes out on the Responses stream, so any client can render it:
every tool call and its full output as `function_call` / `function_call_output` items,
the model's reasoning summaries and text, and the loop's own events as items named
`loop.iteration_start` (with the ledger summary that iteration was given),
`loop.iteration_end` (the tally `should_continue` read) and `loop.report` (the corrected
report). No side channel, no parsing of prose.

`docs/live.html` ("Run it") renders that stream as the wheel below, with the raw items in a
folded event log. In production it asks the authenticated
control API for a short-lived stream ticket, then reads SSE directly from the streaming App
Service. For local development it calls `http://localhost:8088/responses` directly:

```bash
azd ai agent run --no-client          # or: cd src/strong-loop && python main.py
open docs/live.html
```

## The site: three pages, one wheel

1. `/` — **How it works** (public). A recorded run replayed on the wheel: the charter in on
   the left, six stations on a ring (the model proposes outside it, code decides inside it),
   the ledger in the centre, the report out on the right. A one-line caption says who did
   what at every step; step through it, or click any station for the records behind it.
   Built by `tools/make_showcase.py docs/runs/<run>` from `tools/index.template.html`; every
   number comes from the run. Re-record after an engine change (commands in `AGENTS.md`).
2. `/charters.html` — **Write a charter** (signed in). Markdown in the standard shape with
   instant, line-numbered checks (`docs/charter-md.js`, the JavaScript twin of
   `loop/charter_md.py`; both pass `tests/charter_md_cases.json`), the shipped charters as
   worked examples, your CSV's columns checked against the metrics in the browser, then
   "Check it with data →".
3. `/live.html` — **Run it** (signed in). Upload or pick a charter and a dataset, check the
   pairing, read what the role will do (the brief), answer its closed-list questions —
   "needed before start" and "defaults shown" — start, and watch the same wheel driven by
   the live stream (`docs/wheel.js` renders both). The report opens below it, with a line
   per accountability.

`/showcase.html` and `/loop.html` redirect to `/`.

## Customer site and deployment

The live page never receives Azure credentials. Static Web Apps authentication protects the
page and linked control API. The control API creates sessions and returns a short-lived,
session-bound upload ticket; the file route accepts that ticket because Static Web Apps can
challenge raw `PUT` uploads before forwarding the signed-in principal. It still validates the
ticket server-side and only permits the four intake paths. The control API also issues a
60-second stream ticket. A second, unlinked App Service validates that ticket and relays the
Foundry SSE stream directly to the browser; this avoids buffering by the Static Web Apps API
proxy. Both services use the same managed identity. App Service is used because the
subscription's storage policy blocks Flex Consumption's OneDeploy path.

Bootstrap and configure the production pipeline once:

```bash
./scripts/provision-web.sh
./scripts/setup-azure-auth-for-pipeline.sh <github-owner/repository>
```

**Who is using it.** The control API records usage events in Application Insights, with
the Static Web Apps sign-in name (`api/src/telemetry.ts`): `visit` (a signed-in page
opened), `session_created`, `file_uploaded` (which intake file and its size, preset names —
never contents), `pairing_checked`, `pairing_accepted`, `run_started`. `make usage
[DAYS=7]` prints one row per user and the latest events. The Application Insights resource
has local auth disabled, so the SDK sends as the API's managed identity (Monitoring Metrics
Publisher); a connection string alone ingests nothing.

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
| `gates.py` | screens (PII, leakage, forbidden columns, code injection, circular rules), three gates, BH correction. **The only place an Evidence is made.** |
| `derived.py` | what THIS data says may not be tested: constants, copies, functional dependencies, aliases — per (column, metric), per run |
| `stats.py` | the tests, no scipy |
| `charter.py` | the role as a contract — accountabilities with `leads`, decision rights, evidence standards, a `glossary` — plus `authorise_action`, the only door to an Action |
| `charter_md.py` | the same charter as standard Markdown: `parse` (strict, line-numbered errors) and `render` |
| `ledger.py` | append-only JSONL; `summary()` is what a fresh iteration reads |
| `questions.py` | one question per accountability |
| `intake.py` | the pairing: screens, the closed-list questions (blocking and advisory), `resolve` / `settle_advice` |
| `brief.py` | what the persona does on this data: the page, every iteration and the report read the same brief |
| `tools.py` | eight bound operations; three of them certify |
| `models.py` | one env var picks the model |
| `runner.py` | `AgentLoopMiddleware(fresh_context=True)` + `RunScope` |

Deliberately absent: a shell, a sandbox, a filesystem policy, a workflow graph, a run
registry, a front door, fuzzy column matching. Each is a later step if a
run proves it is needed, and not before.

Loop mechanics adapted from the [ralph-playbook](https://github.com/ClaytonFarr/ralph-playbook).
