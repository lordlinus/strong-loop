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
| Leakage is derived from the data per run (`loop/derived.py`), per (column, metric), not listed per charter | The first real export carried the metric's own source column under another name and the loop "found" a tautology (d=6.7, p=1e-289). No list written before seeing the data can cover the next export; `constraints.leakage_features` stays as a human override only. A leak is scoped to the metric it defines: `pillars_met` defines `capability_coverage_pct` and drives `agent_token_pct`. | If a shipped pairing ever shows a false positive in `check`'s `derived screens:` block — tune the guard in `derived.py`, never add a charter list. |
| A gate's outcome is the question's own metric; `driver_effect` handles continuous metrics | A continuous metric made the model pick another binary column as `target` and write findings about the metric it never tested. | — |

## 3. What exists (all verified live, 2026-09-07)

- Steps 1–5: scaffold + APIM routing → trust boundary + tests → the loop → hosted → toolbox.
- Stream contract (`LoopEventStream`), live viewer (`docs/live.html`), "How it works"
  generated from a recorded run (`docs/index.html`, `tools/make_showcase.py`, `docs/runs/`).
- Customer deployment: Standard Static Web App with public replay/deep-dive pages,
  Entra-protected live page, a linked control API, and a separate Linux App Service that
  streams directly to the browser using short-lived signed tickets. Both APIs share the
  same managed identity. GitHub Actions deploys agent, APIs, and site through OIDC.
- 49 tests, no model or network. Both charters pass `check`.
- Results: 2-iteration local run 16 hypotheses / 4 findings / 2 approved actions; deployed
  4-iteration run 34 hypotheses / 9 findings / 4 actions in ~150 s. Hosted agent version 6
  and the full production workflow were verified successfully.
- Uploaded data headers are normalized to safe gate identifiers. If a charter metric still
  has no lexical candidate, TypeSafe ranks one closed set of already-screened columns with
  calibrated `Choice` probabilities and an explicit `none_of_the_above`; the human remains
  the only actor that can accept and ratify the remapping. `python -m loop run --map
  <acc>=<column> --ratified-by <name>` walks the same closed-list path from the CLI.
- Derived screens (2026-09-22, `loop/derived.py`): per run, from the data alone — constant
  columns and metrics, copies and rescalings (Spearman away from the shared tie block),
  functional dependencies both ways, perfect separation, aliases. A leak is (column, metric)
  and is refused only for a hypothesis about that metric; a constant metric withholds its
  question and the run proceeds. Each gate also refuses a RULE that partitions its outcome
  exactly (`circular`), refuses any `target`/`measure` that is not the question's metric,
  and `driver_effect` pools within-stratum mean differences for a continuous metric. Same
  rows under two spellings are one experiment (`Hypothesis.rows_hash`). TypeSafe adds a
  schema-only second tier (copy/component/consequence per column×metric, identifier per
  column): P≥0.70 refuses, 0.35–0.70 warns, code tier wins. On the shipped pairings the
  screens fire only where they should: `web_events` had `is_js_error == (event_name ==
  'javascript_error')` and a traffic-source alias nobody had listed.
- The challenge precondition (2026-09-22, tightened twice from live runs): a finding needs a
  `driver_effect` that RAN (SUPPORTED or REJECTED) on the same `where` AND the same outcome
  column, and a `driver_effect` record whose own `confound_explains_fraction` ≥ 0.5 cannot
  be recorded as a finding — it is its own successful challenge. Every gate stamps its
  outcome (`target` / `measure`) into `Evidence.statistics` so this can be checked.
- Scenario sweep 2026-09-22 (3 iterations each, gpt-5.6-luna): claims 6 findings / 2 approved
  actions with `driver_effect` on continuous `days_to_settle`; portfolio 5 / 5; hr 9 / 5 with
  one BH demotion; web 0 findings, 9 inconclusive — `is_active_user` is nearly constant and
  the report says so. No circular refusals on shipped data; 1–11 duplicates caught per run.
  Presets are now committed files only (`inputs.presets()` skips gitignored paths).
- Domain context enters through the charter, never code (2026-09-22): `glossary` (term →
  meaning here) and `accountabilities[].leads` (where the role looks first). Both reach
  the model verbatim via `get_charter` / the question text, and the glossary rides along
  in the TypeSafe mapping and semantic-screen requests. A play = a decision right with
  the population in its description and the metric it drives as `observation_metric`.
- Genericity probe on an unseen pairing (2026-09-24): IBM Telco churn CSV + a new
  retention charter, no code per role. It broke on typing, not logic: a `Yes`/`No` outcome
  was coerced to all-zero, and numbers-as-text (`TotalCharges` with blanks) blinded the
  numeric screens. `intake.normalize_values` now reads unambiguous yes/no spellings and
  numeric text as numbers at load and lists each in `IntakeReport.recoded`; the smoke
  probe only targets an `ok` metric. `RoleCharter.status` is DRAFT unless a content hash
  exists (it defaulted to SIGNED, so every shipped charter claimed a signature it lacked).
- Frontier model reasons, TypeSafe judges (2026-09-24): semantic tier adds a
  protected-attribute/proxy Noul per column (live: `SeniorCitizen` P=0.97 refused);
  `record_finding` / `propose_action` screen their prose against the cited ledger record
  (§6.6 item 1). Verified live: contradiction refused P=1.00, study-as-action refused
  P=0.93, causal claim and scope overreach warned, clean text clean; 0.7–2 s per call,
  off the event loop (sync tools run via `asyncio.to_thread`).

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
| Live APIs | `app-strong-loop-s56amuculonh4` handles authenticated control calls behind SWA; `app-strong-loop-s56amuculonh4-stream` relays SSE directly using 60-second signed tickets; shared UAMI holds **Foundry User** |
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
- A Static Web Apps linked-backend request buffers SSE until completion even when the Node
  backend flushes headers and writes heartbeats. Keep authentication/session setup on the
  linked API, then stream from an unlinked App Service using a short-lived signed ticket.

## 6. Roadmap — next steps in order, each separately testable

### 6.0 Customer deployment — completed 2026-09-07
GitHub Actions tests first, then deploys the Foundry hosted agent, two App Service API
surfaces, and Static Web App in parallel. The linked control API receives the trusted SWA
principal, creates sessions/files, and issues 60-second HMAC-signed stream tickets. The browser
uses a ticket to call the unlinked stream API directly, avoiding SWA response buffering without
receiving an Azure token. Production timing proved 63 chunks arrived over 38.7 seconds rather
than at completion. App Service is used instead of Flex Consumption because the subscription
policy `StorageAccount_PublicNetwork_Modify` blocks its deployment package path.

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

### 6.6 TypeSafe (System One) — where cheap calibrated judgement fits, in order
**Status 2026-09-22:** item 2 (intake column battery) is built as the semantic tier of
`loop/derived.py` (see §3). Item 3 is built: every missing metric is reviewed, a Noul
`exists` companion gates the menu (< 0.35 withholds it), TypeSafe candidates are appended
after the lexical ones, and the request is cached per charter+schema. Items 1, 4, 5 stand.
**Status 2026-09-24:** item 1 is built (`suggest.review_finding` / `review_action`, called
from `Toolbelt`; `Finding.warnings` / `Decision.warnings` reach `report.json`). The
direction Noul was folded into the support Choice (`contradicts` covers it), and
"asserts causation" warns rather than refuses — the loop's own questions ask about
"drivers". Item 2 gained the protected-attribute/proxy Noul. Open: wide schemas
(> `_MAX_QUESTIONS`) still skip the semantic tier instead of batching.

Reviewed 2026-09-22 against all 18 cookbooks at docs.typesafe.ai. The primitive is one
`system_one(state, questions)` call answering many Choice / Score / Noul questions at once
(~100 ms, ~$0.00005, sd ≈ 0.01 across repeats), with confidence read from how concentrated
the probabilities are. Two rules carry over from intake: TypeSafe never mints Evidence and
never approves an Action; its probabilities may only narrow a menu, add a screen, or attach
a warning. Schema metadata and ledger records may leave the service; row values may not.

1. **Finding and action text screen** (`record_finding`, `propose_action`) — citation_check
   + llm_guardrails. The headline, interpretation and recommendation are the only model
   prose that crosses the rail unchecked into `report.json`. State = the Evidence record
   (gate, spec, effect, p, warnings) plus the accountability's direction; questions:
   Choice{supports, contradicts, says_nothing} on the headline, Noul "asserts causation"
   (subgroup gates are associational), Noul "claimed direction matches the statistics", and
   for actions Noul "describes further study rather than something done to the group"
   (SYSTEM rule 9 is prompt-only today). Route by confidence: contradicts / study ≥ 0.8 →
   refuse with the reason; middle band → record with a warning the report shows; API
   unavailable → record as today. One `_ask` seam, tested like `suggest.py`.
2. **Intake column screens** (`intake.pair`) — classifying_rag_passages battery +
   sde_cascade. `is_sensitive_column` is a word list: `Age` in `hr_attrition.csv` walks
   through, and leakage is declared by hand in every charter (`Adopted` next to
   `product_adopted_flag` was a real case). Per analysable column, from schema metadata
   only: Noul identifier / protected attribute or proxy / post-outcome leak of the metric /
   free text. The output is a *proposal* in the IntakeReport the human already ratifies,
   never a silent block — a false positive would remove a legitimate driver.
3. **Harden `suggest.py`** — semantic_find pairs its Choice with a Noul `exists`; a
   `none_of_the_above` option inside a 200-way Choice is diluted by the very distribution
   the confidence is read from. Add one Noul per accountability ("does any listed column
   measure this metric?") and withhold the menu below ~0.35. Cache by (charter hash, schema
   hash) as the cookbooks do with `@json_cache`: `settle()` re-asks on every `awaiting`
   turn until `accept.json` lands. The 255-option Choice limit in `_MAX_COLUMNS` is not in
   the SDK docs; the largest cookbook Choice is 218 options.
4. **Ledger summary reranking** — rerank_typesafe. `summary(max_items=8)` is recency; a Noul
   "does this record bear on open question Q" per (record, question) picks the eight that
   matter. Only once a recorded run shows the agent re-testing something older than eight.
5. **Text-feature gate** — autoresearch_feature_discovery, unchanged position: benchmark
   before building, and none of the shipped datasets has a narrative column. If built:
   charter-declared text column and target, frozen dev/test split, cached answers, the
   cookbook's own four-Noul candidate screen and `MIN_SPREAD` flatness check, held-out
   improvement as the gate's statistic, and `gates.py` alone mints the Evidence.

Not worth doing: gate/spec selection via function_calling (the model already receives the
spec schema and a refusal explains itself); hypothesis dedupe beyond `fingerprint` (a gate
call costs milliseconds); replacing the convergence counters (the ledger already answers).

### 6.7 Intake clarification questionnaire — next, in this order
Why: the adoption runs (2026-09-22) spent three model runs discovering what four questions to
a person would have settled before the first: which column is hosted-agent adoption, whether
an all-zero metric should be withheld, whether a period exists, whether `pillars_met` counts
agent-runtime usage. Intake already asks one kind of question (metric mapping) as a closed
list a person picks from and ratifies. This extends that mechanism; it does not add a chat.

Question kinds, all generic, all generated by code from charter + schema, never free text:
1. **Metric mapping** — exists (`Clarification`).
2. **Glossary binding** — for each `glossary` term no column name matches: "which column
   measures *hosted-agent adoption*?" The pick becomes a remap or a lead.
3. **Suspected leakage** — every `Derived.suspected` entry (semantic 0.35–0.70) becomes a
   yes/no: "is `agent_tokens` computed from the same quantity as agent token share?" Yes →
   `Derived.leaks` (human-confirmed); no → cleared for this schema.
4. **Grain and time** — code: is the identifier unique per row; does any column parse as a
   date/period? None → one advisory question, and every report of that pairing carries
   "snapshot; findings are associations only".
5. **Thresholds behind glossary phrases** — "heavy LLM API user" needs a cutoff; offer what
   code can compute (`> 0`, `> median`, top quartile). The pick becomes a lead.
6. **Constraint choices** — `age` and similar attributes the word list does not block:
   "allow in targeting rules for this role?" Default no; the answer lands in constraints.
7. **Weak metrics** — the R0 near-constant warning as a question: proceed / withhold / bring
   richer data.

Every answer lands somewhere typed (remap, glossary entry, confirmed leak, lead, constraint,
withheld question) in `intake/pairing.json`, ratified by name, keyed by charter fingerprint +
schema hash so the same export never asks twice. Blocking kinds (1, 2, 6) gate the run;
advisory kinds (3, 4, 5, 7) are shown, recorded, and carried into the report.

Where Jev fits (one batched `system_one` call per intake, schema metadata only, cached):
- **Ask or default** — a Noul per candidate question: "settleable from the charter and column
  metadata alone?" High → propose the default, do not ask. Low → ask. Keeps the questionnaire
  to the three or four decisions a person must make (the SDE-cascade pattern).
- **Menu order** — a Choice over candidates with the glossary in the state, so `AHR_adoption`
  ranks first for hosted-agent adoption and `agent_tokens` does not (semantic_find pattern:
  Choice + Noul `exists`, already built for kind 1).
- **Post-answer check** — given the bindings, "is `model_usage_acr > 0` now a definition of
  the metric?" so a circular rule is caught before an iteration is spent on it.
Rules unchanged: Jev proposes and ranks; a person picks from a closed list and signs; the
code tier of `derived.py` wins wherever data can prove what Jev only suspects.

Build order (each step testable offline through the `_ask` / `_ask_nouls` seams):
1. `Clarification` gains `kind` and a typed `applies_to`; `intake.resolve` applies each kind;
   `pairing.json` records answers by kind. No model.
2. Grain/time detector and the constraint question in code; the advisory line in `finalise`.
3. The Jev pass (ask-or-default Noul + ranking Choice), cached per charter + schema.
4. Surface in `check` (prose + `--json`) and the hosted `awaiting` state, which already
   renders clarifications; CLI answers through `--map`-style flags per kind.

### 6.8 Close the loop on the loop
- **Outcomes.** Every action carries an `observation_plan`; nothing writes an `Outcome` yet.
  Record what happened to the proposed cohorts after the observation lag and feed it back —
  the only path from associations to evidence about the plays, and the way autonomy levels
  were meant to be earned. Needs the time dimension in the data (§6.7 kind 4).
- **Recorded runs as regression fixtures.** The ledgers under `/tmp/sl-runs*` from
  2026-09-22 capture every failure mode found that week (tautology, target substitution,
  same-rows duplicate, refused challenge counted, cross-outcome challenge, proxy recorded
  directly). Replay them through `record_finding` and the gates in tests, no model needed.
- **Stability.** Two runs of one charter chose different segments to test. Run each scenario
  more than once and keep the findings that recur; §6.5's grader should report recurrence.
- **Finding-text screen** — §6.6 item 1, built 2026-09-24 (`suggest.review_finding` /
  `review_action`).

### 6.9 A simpler UI — three pages, one wheel (planned 2026-09-24)
Five pages (Overview, See it, Understand it, Charters, Try it live) become three, and the
wheel from `showcase.html` — charter in, six stations on a ring, report out, ledger in the
centre — is the only picture of the loop. It already takes replay and the live stream
through one `apply(event)`.

1. **How it works** (`index.html`, public) — the wheel replaying a recorded run
   (`tools/make_showcase.py`, every number from the run). Plain verbs on its face, gate
   names and spec JSON behind clicks; a one-line caption narrates each event; step
   controls; refusals as visible as successes. `loop.html`'s deep dive moves into the
   station click-throughs. Acceptance: someone new to the project watches it for two
   minutes, unaided, and can say what the model does, what it may not do, and where a
   finding's confidence comes from.
2. **Write a charter** (`charters.html`) — charters authored as **Markdown in a standard
   format**: flat front matter (role, version, extends, signature), fixed `##` sections,
   `- key: value` bullets. Parsed by fixed rules in code, never a model — the charter is
   signed authority. YAML stays the source for shipped charters; Markdown is rendered from
   it for display. Annotated template, shipped examples, "start from my CSV headers",
   instant checks from a JavaScript copy of the parser, then hand-off to page 3. The two
   parsers share one case file (`tests/charter_md_cases.json`), so they cannot drift.
3. **Run it** (`live.html`) — upload charter (`.md`/`.yaml`) and CSV, the intake check,
   sign, then the same wheel fed by the Responses stream; the raw timeline stays as a
   collapsed event log; the report opens from the output card. Verify time from "start" to
   the first station lighting up on event timestamps, locally and deployed.

**Status 2026-09-24:** step 1 is built — `loop/charter_md.py`, `load_charter`/`write_charter`
read and write `.md`, `loop render`, intake + both upload allow-lists take
`intake/charter.md` (both formats at once is refused), 27 shared parse cases in
`tests/charter_md_cases.json`. Every shipped charter round-trips with identical content and
identical `check` output; fingerprints differ only by YAML folded-scalar whitespace, so a
YAML signature does not carry to Markdown — `sign --out x.md` signs the Markdown form.

**Status 2026-09-24 (later):** steps 2–6 are built. `docs/wheel.js`/`wheel.css` (mount,
`adapter` for the Responses stream, `fromTrace` for a recorded run, `narrate` for the
caption) drive page 1 and page 3; `docs/charter-md.js` passes all shared cases under node;
`/showcase.html` and `/loop.html` 301 to `/`. Page 1 re-recorded on the current engine (26
tests, 4 findings, 3 approved actions, 2 claim-screen warnings). Verified in a browser
against the local agent: page 2 → page 3 → check → accept → a 2-round run, first station lit
4.1 s after start, report after 82 s, no console errors.

Order: (1) Markdown charter format in Python — `loop/charter_md.py`, `load_charter` reads
`.md`, intake accepts `intake/charter.md`, `loop charter render`; (2) extract the wheel into
`docs/wheel.js`; (3) page 1; (4) page 2 with the JS parser; (5) page 3; (6) nav, SWA routes,
redirects for the retired pages, docs.

## 7. Open questions

- How does domain knowledge enter without becoming code? Answered 2026-09-22: `glossary` and
  `leads` in the charter, plays as decision rights, and (§6.7) a closed-list questionnaire
  at intake for what the charter cannot know until it meets the data.
- What is the right budget? 4 iterations found 9 findings on the claims charter; the
  digital-analytics charter in the old repo found 0 in 6. Convergence should be tuned from
  recorded runs.
- Should `web` stay in the toolbox for an analyst over private data? It is context-only, but
  it is also an egress path.
