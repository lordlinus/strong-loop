# PLAN.md — why this repo is shaped the way it is, and what comes next

`AGENTS.md` is the mechanics. `README.md` is how to run it. This file is the reasoning and
the roadmap: read it before proposing a change, and update §6 when a step lands.

## 1. The idea

A role charter and a dataset in; evidence-backed findings and authorised actions out. The
agent proposes hypotheses. A fixed statistical gate is the only thing that can turn one into
evidence. An append-only ledger on disk is the only memory between iterations, and every
iteration starts from an empty context. It is the Ralph loop from autonomous coding with a
statistical gate in place of a test suite — and that substitution is the point: a test can
be rewritten to pass, a gate cannot.

Two invariants carry everything, and both are enforced by tests rather than documented:

1. Only `loop/gates.py` constructs an `Evidence` (`tests/test_invariant_evidence.py`).
2. Only `authorise_action` approves an `Action`; `loop/tools.py` is its only caller.

## 2. Decisions, and why

| Decision | Why | Revisit when |
|---|---|---|
| Fresh repo, not a refactor of `embedded-intelligence-platform` | The old repo carried two loop implementations, a dead hosted-tool layer and ~250 lines of filesystem confinement that failed open twice. The asset worth keeping (gates, charter, ledger, types) was ~1,600 lines; the loop itself ~100. | Never; the old repo remains the reference for intake, glossary, Fabric grounding. |
| One SDK path: agent-framework `Agent` + `AgentLoopMiddleware(fresh_context=True)` | The middleware IS the Ralph property, natively. The Copilot harness gave a shell, and the shell needed a boundary we had to build and maintain; the 22–37k-token harness prefix per iteration came with it. | If a run proves the agent needs to *look* at data beyond `get_data_profile` — then hosted code interpreter, not a shell (§6.3). |
| No shell, no sandbox, no filesystem policy | Nothing to confine. The agent has eight bound tools and a toolbox; the gate scores the data on its behalf. | Same trigger as above. |
| Tokens through the APIM gateway; no `deployments:` in `azure.yaml` | The Foundry account had zero model deployments and regional quota was exhausted. APIM already fronts `/openai/v1` (Responses) and `/anthropic`; `LOOP_MODEL` swaps models with one variable. | When a project model deployment exists — it unlocks `FoundryChatClient`, hosted code interpreter and Foundry Memory. |
| Foundry hosted-agent template shape | `azd ai agent run` / `invoke` / `up` work unchanged; `project:` carries everything the container needs. | — |
| `ResponsesHostServer(history_source="agent")` | Default hosting prepends the conversation transcript; a second turn replayed the first run into every fresh-context iteration and the model returned 400. The ledger is the memory, the transcript is not. Every turn is one run. | If a conversational front door is added (§6.4), that agent gets history; the analyst never does. |
| Finalise in a `ContextProvider.after_run` (`after_run_once_per_turn=True`), not in `should_continue` | The middleware's iteration cap fires *before* the predicate, so on the last permitted run the predicate never runs. | — |
| Convergence reads the ledger, never the model's text | The model's summary is not evidence of progress. Idle (`IDLE_PATIENCE=2`) and stagnant (`PATIENCE=4`) after `MIN_ITERATIONS=3`. | Tune from recorded runs, not intuition. |
| `Ledger.summary(max_items=8)` | It is loaded into every fresh context; at 25 items it cost ~4k tokens per iteration in the old repo. | — |
| Toolbox holds skills + `web` only; no `code_interpreter`, no certifying ops | A toolbox attaches to the agent and reaches every charter. A sandbox is a per-charter grant; certification is per run. | — |
| Loop events streamed as `function_call`/`function_call_output` items named `loop.*` | Every Responses client already renders that shape; no side endpoint, no parsing prose. Tool calls are already native items and are not re-emitted. | — |
| Runs partitioned `runs/<user>/<session>/<role>-<stamp>-<conversation>` | Ids from the platform request context; the report carries them. Per-user isolation itself is the platform's (protocol 2.0.0 derives the user from the Entra token). | Archive at finalise when audit across sessions is needed (§6.2). |

## 3. What exists (all verified live, 2026-09-07)

- Steps 1–5: scaffold + APIM routing → trust boundary + tests → the loop → hosted → toolbox.
- Stream contract (`LoopEventStream`), live viewer (`docs/live.html`), explainer page
  generated from a recorded run (`docs/loop.html`, `tools/make_loop_doc.py`, `docs/runs/`).
- Customer deployment: Standard Static Web App with public replay/deep-dive pages,
  Entra-protected live page, and a linked Linux App Service streaming proxy using managed
  identity. GitHub Actions deploys agent, API, and site through environment-scoped OIDC.
- 49 tests, no model or network. Both charters pass `check`.
- Results: 2-iteration local run 16 hypotheses / 4 findings / 2 approved actions; deployed
  4-iteration run 34 hypotheses / 9 findings / 4 actions in ~150 s. Hosted agent version 6
  and the full production workflow were verified successfully.

## 4. Deployment coordinates

| | |
|---|---|
| Subscription / RG | `75f2a33a-540e-4d0f-bd91-5681b79baa70` / `rg-aps-underwriting` (southeastasia) |
| Foundry account / project | `cog-vo5ai77uq5yno` / `ai-project-aps-underwriting` (created by `azd up`; **not** the older `cog-x5cjyqlvtwjno`) |
| Agent | `strong-loop`, responses protocol 2.0.0; endpoint in azd env `AGENT_STRONG_LOOP_RESPONSES_ENDPOINT` |
| Agent managed identity | `a3921277-2d07-4fe7-97f2-72a55048c0ff`, holds **Foundry User** on the account (needed to read skill bodies) |
| Toolbox | `strong-loop-toolbox` v1, endpoint in azd env `TOOLBOX_STRONG_LOOP_TOOLBOX_MCP_ENDPOINT` |
| Models | APIM `apim-ssattiraju-01`; gateway + key from `~/.config/azure-apim/apim-ssattiraju-01.env` locally, azd env in the container. Default `gpt-5.6-luna`. |
| azd environment | `strong-loop` (`.azure/strong-loop/.env`, gitignored) |
| Customer site | `https://wonderful-smoke-0d5632100.3.azurestaticapps.net` (Standard, East Asia) |
| Live API | `app-strong-loop-s56amuculonh4` linked behind SWA `/api/*`; UAMI holds **Foundry User** on the project |
| GitHub deployment | `https://github.com/lordlinus/strong-loop/actions/workflows/deploy.yml`, OIDC through environment `production` |

## 5. Verified platform gotchas (each cost real time; do not re-learn)

- `azd ai agent run` injects every `environmentVariables` name from `azure.yaml` as `""` when
  the azd env has no value. `models.load_env` treats empty as unset.
- `azd up` with `infra.provider: microsoft.foundry` provisions a **new** account per azd env
  even with project ids pre-set. To reuse a project: `azd ai agent init --project-id`.
- The `azd ai skill` and `azd ai toolbox` extensions read different project-endpoint
  variables (`AZURE_AI_PROJECT_ENDPOINT` vs `FOUNDRY_PROJECT_ENDPOINT`). Pass `-p` explicitly.
- Toolbox skills are MCP **resources** (`skill://index.json`), not tools; surface them with
  `FoundryToolbox.as_skills_provider(disable_load_skill_approval=True, ...)`. The MCP endpoint
  needs the `Foundry-Features: Toolboxes=V1Preview` header.
- The hosted agent's managed identity has **no** role by default; reading a skill body failed
  with `McpError('Failed to read resource.')` until it got Foundry User on the account.
- `x-ms-user-identity` (`azd ai agent invoke --user-identity`) is delegation for a middle
  tier and requires the data action `…/agents/endpoints/UserIdentityImpersonation/action`,
  which no built-in role has → 403. Direct callers are isolated per user by the platform anyway.
- `AgentLoopMiddleware`'s cap fires before `should_continue`; middleware `context.result`
  for tools arrives as `list[Content]` with the JSON in `.text`.
- The OTel Azure distro probes IMDS on a laptop; `main.py` disables OTel when
  `FOUNDRY_HOSTING_ENVIRONMENT` is unset.
- A client that disconnects mid-run leaves a run folder without `report.json`; finalise did
  not run. §6.1 is the fix.
- `azd ai agent files download` takes one path and writes into the azd project dir.

## 6. Roadmap — next steps in order, each separately testable

### 6.0 Customer deployment — completed 2026-09-07
GitHub Actions tests first, then deploys the Foundry hosted agent, linked API, and Static Web
App in parallel. The live route is Entra-protected and the browser receives no Azure token.
The API is App Service rather than Flex Consumption because the subscription policy
`StorageAccount_PublicNetwork_Modify` forces deployment storage public access off, which
made OneDeploy fail before code upload. App Service preserves streaming without weakening
that policy.

### 6.1 Resilient runs (crash recovery + steering) — designed, not built
Wrap the loop in `@task` from `azure.ai.agentserver.core.tasks` (works inside agent-framework's
`ResponsesHostServer`; local filesystem provider under `~/.agentserver`). Durable progress is
the ledger plus a small watermark in the run dir. On `entry_mode == "recovered"` run only the
remaining iterations; skip actions already in the ledger. Then `@multi_turn_task(steerable=True)`:
`should_continue` checks `ctx.cancel` at the iteration boundary so a human can redirect mid-run.
Then the hosted turn becomes start / check / read tools over the ledger. Do **not** use
`resilient_background=True`: it requires a `WorkflowAgent`. Caveat: installed core 2.1.0 lacks
the documented `ctx.metadata`; the watermark file covers it.

### 6.2 Archive at finalise
Copy ledger + report to storage that outlives the hosted session, keyed
user / session / run — Foundry state store (zero infra, local file fallback) or Blob (queryable).

### 6.3 Hosted code interpreter (needs a project model deployment)
Inner `Agent` on `FoundryChatClient` with code interpreter over an uploaded exploration split,
exposed to the loop via `Agent.as_tool()` as a `SANDBOX`-reach tool the charter must grant
(`exploration_share > 0`, as in the old repo). Never in the toolbox.

### 6.4 A conversational front door
A second hosted agent with history, Foundry Memory for user preferences, and start / check /
read tools over the analyst. Only if a minutes-long analyst turn proves to be a problem.

### 6.5 Evaluation over the ledger
`azd ai agent eval` or a grader that scores runs from `ledger.jsonl` (yield, challenge rate,
demotions), so prompt and convergence changes are measured, not felt.

## 7. Open questions

- How does domain knowledge enter without becoming code? Charters + skills today; the old
  repo's glossary/grounding is the reference if it is needed.
- What is the right budget? 4 iterations found 9 findings on the claims charter; the
  digital-analytics charter in the old repo found 0 in 6. Convergence should be tuned from
  recorded runs.
- Should `web` stay in the toolbox for an analyst over private data? It is context-only, but
  it is also an egress path.
