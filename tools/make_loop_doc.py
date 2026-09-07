"""make_loop_doc.py — build docs/loop.html from a recorded run.

    python tools/make_loop_doc.py docs/runs/<run_dir> --out docs/loop.html

Everything on the page is read from artefacts: the charter YAML, the dataset, the run's
`ledger.jsonl`, `trace.log`, `iterations/*.md` and `report.json`, and the engine's own
source. Nothing is typed in by hand, so the page cannot drift from what the loop does.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import inspect
import json
import pathlib
import sys
import textwrap

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICE = ROOT / "src" / "strong-loop"
sys.path.insert(0, str(SERVICE))

from loop import gates, runner, tools  # noqa: E402
from loop.charter import authorise_action, load_charter  # noqa: E402
from loop.ledger import Ledger  # noqa: E402
from loop.types import Verdict  # noqa: E402

E = html.escape


# ------------------------------------------------------------------------------ data
def load_run(run_dir: pathlib.Path) -> dict:
    ledger = Ledger(run_dir)
    trace = [json.loads(line) for line in (run_dir / "trace.log").read_text().splitlines() if line.strip()]
    report = json.loads((run_dir / "report.json").read_text())
    headers = {int(p.stem): p.read_text() for p in sorted((run_dir / "iterations").glob("*.md"))}
    return {"ledger": ledger, "trace": trace, "report": report, "headers": headers}


def by_id(ledger: Ledger) -> dict:
    return {getattr(obj, "id"): obj for _, obj in ledger if hasattr(obj, "id")}


# ------------------------------------------------------------------------------ html bits
def verdict_badge(verdict: str) -> str:
    return f'<span class="verdict v-{verdict.lower()}">{E(verdict)}</span>'


def fmt_p(p) -> str:
    if p is None:
        return "—"
    return f"{p:.2e}" if p < 0.001 else f"{p:.4f}"


def code_block(text: str, lang: str = "") -> str:
    return f'<pre class="code {E(lang)}"><code>{E(text)}</code></pre>'


def source_of(obj) -> str:
    return textwrap.dedent(inspect.getsource(obj))


def eyebrow(author: str, artefact: str) -> str:
    return (f'<p class="eyebrow"><span class="who who-{author}">{E(author)}</span>'
            f'<span class="artefact">{E(artefact)}</span></p>')


# ------------------------------------------------------------------------------ sections
def sec_hero(charter, run, data) -> str:
    ledger = run["ledger"]
    ev = ledger.all("evidence")
    hyp = by_id(ledger)
    first = next((e for e in ev if e.verdict == Verdict.SUPPORTED), ev[0])
    h = hyp[first.hypothesis_id]
    report = run["report"]
    return f'''
<header class="hero">
  <p class="kicker">strong-loop · a recorded run · {E(charter.role)} over {E(str(data.attrs.get("name","")))} ({len(data):,} rows)</p>
  <h1>The agent proposes.<br><span class="code-ink">Code disposes.</span></h1>
  <p class="lede">An autonomous analyst that can say anything it likes on the left of this line, and
  nothing at all on the right. Every claim below is a real record from one run: the model wrote the
  hypothesis, a fixed statistical gate wrote the verdict, and an append-only ledger on disk is the
  only memory carried from one iteration to the next.</p>
  <div class="lanes hero-lanes">
    <div class="lane model">
      <p class="lane-label">the model wrote</p>
      <p class="statement">“{E(h.statement)}”</p>
      <p class="spec mono">{E(h.kind)} · where <code>{E(h.spec.get("where",""))}</code></p>
    </div>
    <div class="rail"><span class="stamp"></span></div>
    <div class="lane code">
      <p class="lane-label">code decided</p>
      <p class="verdict-line">{verdict_badge(first.verdict.value)}</p>
      <p class="stats mono">effect {first.effect_size:.2f}× · p {fmt_p(first.p_value)} · n {first.sample_size:,}</p>
    </div>
  </div>
  <dl class="run-facts mono">
    <div><dt>iterations</dt><dd>{report["iterations_run"]}</dd></div>
    <div><dt>hypotheses tested</dt><dd>{report["hypotheses_tested"]}</dd></div>
    <div><dt>findings recorded</dt><dd>{len(report["findings"])}</dd></div>
    <div><dt>actions authorised</dt><dd>{sum(1 for a in report["actions"] if a["status"] in ("approved","proposed"))}</dd></div>
    <div><dt>demoted at finalise</dt><dd>{report["demoted_by_multiple_testing"]}</dd></div>
  </dl>
</header>'''


def sec_shape() -> str:
    return f'''
<section id="shape" class="wide">
  {eyebrow("code", "the loop, in one diagram")}
  <h2>One turn is one run. One run is N fresh contexts over one ledger.</h2>
  <pre class="diagram mono" aria-label="loop diagram">
 charter (human)  ─┐                      ┌──────────────── iteration i of N ───────────────┐
                   ├─▶ questions ─▶ ledger │  empty context                                  │
 dataset (human)  ─┘                  ▲   │  + system prompt + header (ledger summary)       │
                                      │   │  model: read progress · pick a question ·        │
                                      │   │         propose hypotheses ─────────────▶ GATE   │
                                      │   │  code:  screen · test · verdict ─▶ Evidence ─────┼──▶ ledger
                                      │   │  model: challenge · record_finding · propose     │
                                      │   │  code:  authorise_action ─▶ Action ──────────────┼──▶ ledger
                                      │   └─────────────────────────────────────────────────┘
                                      │        should_continue? reads the LEDGER, never the text
                                      └── finalise: BH correction across the whole run ─▶ report.json
  </pre>
  <p>Three things carry the whole design. The <strong>charter</strong> is a contract a human signed, and
  the only source of authority. The <strong>gate</strong> is fixed code, the only thing that can turn a
  claim into evidence. The <strong>ledger</strong> is the only memory: each iteration starts with an
  empty context and is handed a summary of the ledger, which is what stops a long run degrading into
  a long conversation. The pattern is the Ralph loop from autonomous coding, with a statistical gate
  in place of a test suite, and the difference matters: a test can be rewritten to pass, a gate cannot.</p>
</section>'''


def sec_charter(charter, charter_path: pathlib.Path) -> str:
    raw = charter_path.read_text()
    reads = [
        ("accountabilities", "questions.py → one question per accountability; tools.py → the gate's target column is the accountability's metric"),
        ("decision_rights", "charter.authorise_action → the only action types a role may propose, their blast-radius caps and autonomy levels"),
        ("evidence_standards", "gates.decide → min_sample_size, max_p_value, min_effect_size; gates.apply_multiple_testing → the correction; tools.record_finding → requires_confound_control"),
        ("evidence_overrides", "charter.standard_for(action_type) → a stricter bar for a costlier action"),
        ("constraints", "gates.screen → forbidden, leakage and sensitive columns are refused before any test runs"),
        ("status / content_hash", "charter.load_charter → a signed charter that was edited afterwards refuses to load"),
    ]
    rows = "".join(f'<tr><td class="mono">{E(k)}</td><td>{E(v)}</td></tr>' for k, v in reads)
    return f'''
<section id="charter">
  {eyebrow("human", "input 1 · the role charter")}
  <h2>A role, written as a contract rather than a prompt</h2>
  <p>The charter says what the role is <em>accountable</em> for, what it <em>may do</em>, what counts
  as <em>proof</em>, and what it <em>must not touch</em>. It deliberately cannot say which questions to
  ask or which analyses to run. This is <code>{E(charter_path.name)}</code> exactly as the run loaded it.</p>
  <details open><summary>charters/{E(charter_path.name)}</summary>{code_block(raw, "yaml")}</details>
  <h3>Which code reads which key</h3>
  <table class="reads"><thead><tr><th>key</th><th>read by</th></tr></thead><tbody>{rows}</tbody></table>
  <p class="aside">Nothing in the engine mentions claims, customers or any other domain. The same code
  ran a portfolio charter over a customer file with zero changes; that is the test of whether the
  loop is generic.</p>
</section>'''


def sec_data(charter, data: pd.DataFrame, data_path: pathlib.Path) -> str:
    prof = tools.profile(data, charter)
    measures = prof["accountability_measures"]
    mrows = ""
    for name, m in measures.items():
        if m.get("kind") == "rate":
            desc = f'base rate {m["base_rate"]:.3%} · {m["positives"]:,} of {m["n"]:,}'
        elif m.get("kind") == "numeric":
            desc = f'mean {m["mean"]:,} · median {m["median"]:,} · n {m["n"]:,}'
        else:
            desc = m.get("status", "")
        mrows += f'<tr><td class="mono">{E(name)}</td><td>{E(m.get("kind",""))}</td><td>{E(desc)}</td></tr>'
    crows = ""
    for name, c in prof["columns"].items():
        shape = (f'range {c["range"][0]}–{c["range"][1]} · median {c["median"]}' if "range" in c
                 else "values " + ", ".join(c.get("values", [])[:6]))
        crows += f'<tr><td class="mono">{E(name)}</td><td class="mono">{E(c["dtype"])}</td><td>{c["n_unique"]}</td><td>{E(shape)}</td></tr>'
    excluded = ", ".join(prof["excluded_columns"]) or "none"
    leak = ", ".join(prof["leakage_columns"]) or "none"
    return f'''
<section id="data">
  {eyebrow("human", "input 2 · the dataset")}
  <h2>{E(data_path.name)}: {len(data):,} rows, {len(data.columns)} columns</h2>
  <p>The agent never opens this file. It sees the profile below through <code>get_data_profile</code>,
  and the gate scores the rows on its behalf. The charter's metrics must name real columns, exactly,
  or the run refuses to start.</p>
  <h3>Today's level of each accountability metric</h3>
  <table><thead><tr><th>metric</th><th>kind</th><th>level</th></tr></thead><tbody>{mrows}</tbody></table>
  <h3>Columns the agent may reason with</h3>
  <table class="cols"><thead><tr><th>column</th><th>dtype</th><th>distinct</th><th>shape</th></tr></thead><tbody>{crows}</tbody></table>
  <p class="aside">Excluded by the standing screens as identifiers or protected attributes: <span class="mono">{E(excluded)}</span>.
  Named by the charter as leaking the outcome, so refused in any test: <span class="mono">{E(leak)}</span>.</p>
</section>'''


def sec_questions(run) -> str:
    qs = run["ledger"].all("question")
    items = "".join(
        f'<li><span class="mono qid">{E(q.id)}</span><p>{E(q.text)}</p><p class="why">{E(q.why_it_matters)}</p></li>'
        for q in sorted(qs, key=lambda q: q.priority))
    return f'''
<section id="questions">
  {eyebrow("code", "the ledger's first records · questions")}
  <h2>The agent inherits its questions; it does not choose them</h2>
  <p>Before any model call, one question per accountability is compiled from the charter and appended
  to the ledger. The agent may pick which open question to work, and may not invent a new one.</p>
  <ol class="questions">{items}</ol>
</section>'''


def sec_context(run) -> str:
    headers = run["headers"]
    blocks = "".join(
        f'<details {"open" if n == 2 else ""}><summary>iterations/{n}.md — the header iteration {n} was given</summary>{code_block(text, "md")}</details>'
        for n, text in headers.items())
    return f'''
<section id="context">
  {eyebrow("code", "what an iteration sees")}
  <h2>Every iteration starts from nothing, plus the ledger</h2>
  <p>Each iteration is a new context: the system prompt, the human's steer, and a header that
  carries the ledger's summary. Nothing else. Iteration 2 does not remember iteration 1; it reads
  what iteration 1 wrote. That is why the summary is capped at eight recent tests, and why the agent's
  first instruction is to call <code>get_progress</code>.</p>
  <details><summary>runner.SYSTEM — the system prompt, verbatim</summary>{code_block(runner.SYSTEM, "md")}</details>
  {blocks}
  <p class="aside">The middleware doing this is agent-framework's <code>AgentLoopMiddleware(fresh_context=True)</code>.
  The header is injected by a context provider before every run, and the run's eight tools are bound
  to the ledger the same way.</p>
</section>'''


def timeline(run, charter) -> str:
    ledger = run["ledger"]
    objs = by_id(ledger)
    out = []
    for ev in run["trace"]:
        kind = ev["event"]
        if kind == "iteration_start":
            out.append(f'<li class="iter-start"><span class="mono">iteration {ev["iteration"]} of {ev["max_iterations"]}</span> — empty context, ledger summary injected</li>')
            continue
        if kind == "iteration_end":
            out.append(f'<li class="iter-end"><span class="mono">tested {ev["tested"]}, newly supported {ev["newly_supported"]}</span> — read from the ledger by <code>should_continue</code>; stagnant {ev["stagnant"]}, idle {ev["idle"]}</li>')
            continue
        if kind == "report":
            out.append(f'<li class="iter-end"><span class="mono">finalise</span> — {ev["hypotheses_tested"]} hypotheses corrected across the run, {ev["demoted"]} demoted, report.json written</li>')
            continue
        tool = ev["tool"]; args = ev.get("args") or {}; res = ev.get("result") or {}
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except ValueError:
                res = {"status": res}
        if not isinstance(res, dict):
            res = {}
        if tool == "test_hypothesis":
            h = objs.get(res.get("hypothesis_id")); e = objs.get(res.get("evidence_id"))
            if h is None or e is None:
                left = f'<p class="tool">{E(tool)}</p><p class="statement">{E(args.get("statement",""))}</p>'
                right = f'<p class="muted">{E(res.get("status",""))} — {E(res.get("message",""))}</p>'
            else:
                spec = ", ".join(f'{k}={v!r}' for k, v in h.spec.items())
                left = (f'<p class="tool">{E(tool)} · {E(h.kind)}</p><p class="statement">“{E(h.statement)}”</p>'
                        f'<p class="spec mono">{E(spec)}</p>')
                stats = ""
                if e.verdict != Verdict.REFUSED:
                    eff = f'{e.effect_size:.2f}×' if e.effect_size is not None else "—"
                    stats = f'<p class="stats mono">effect {eff} · p {fmt_p(e.p_value)} · n {e.sample_size:,}</p>'
                    if e.adjusted_p_value is not None:
                        stats += f'<p class="stats mono muted">adjusted p {fmt_p(e.adjusted_p_value)} after BH</p>'
                    extra = {k: v for k, v in e.statistics.items() if k in ("control", "confound_explains_fraction", "adjusted_odds_ratio", "min_detectable_lift", "cohens_d")}
                    if extra:
                        stats += f'<p class="stats mono muted">{E(", ".join(f"{k} {round(v,3) if isinstance(v,(int,float)) else v}" for k, v in extra.items()))}</p>'
                reason = f'<p class="reason">{E(e.refusal_reason)}</p>' if e.refusal_reason else ""
                warns = "".join(f'<p class="reason muted">{E(w)}</p>' for w in e.warnings[:2])
                right = f'<p class="verdict-line">{verdict_badge(e.verdict.value)}</p>{stats}{reason}{warns}'
            out.append(f'<li class="rec gate-rec"><div class="lane model">{left}</div><div class="rail"><span class="stamp"></span></div><div class="lane code">{right}</div></li>')
        elif tool == "record_finding":
            f = objs.get(res.get("finding_id"))
            left = f'<p class="tool">{E(tool)}</p><p class="statement">evidence <span class="mono">{E(args.get("evidence_id",""))}</span></p>'
            if f is not None:
                left += f'<p class="statement">“{E(f.headline)}”</p>'
            status = res.get("status", "")
            if status == "recorded":
                right = f'<p class="verdict-line"><span class="verdict v-recorded">RECORDED</span></p><p class="stats mono">confidence {res.get("confidence")} — derived from the evidence, not self-assessed</p>'
            else:
                right = f'<p class="verdict-line"><span class="verdict v-refused">{E(status.upper() or "REFUSED")}</span></p><p class="reason">{E(res.get("message",""))}</p>'
            out.append(f'<li class="rec"><div class="lane model">{left}</div><div class="rail"><span class="stamp"></span></div><div class="lane code">{right}</div></li>')
        elif tool == "propose_action":
            a = objs.get(res.get("action_id"))
            left = f'<p class="tool">{E(tool)} · {E(args.get("action_type",""))}</p>'
            if a is not None:
                d = objs.get(a.decision_id)
                if d is not None:
                    left += f'<p class="statement">“{E(d.recommendation)}”</p>'
                left += f'<p class="spec mono">blast radius {E(json.dumps(a.blast_radius))} · observe {E(json.dumps(a.observation_plan))}</p>'
                findings = [objs[i] for i in d.finding_ids if i in objs] if d else []
                auth = authorise_action(charter=charter, action_type=a.action_type, findings=findings,
                                        evidence_by_id={e.id: e for e in ledger.all("evidence")},
                                        requested_blast_radius=a.blast_radius, observation_plan=a.observation_plan)
                reasons = "".join(f'<p class="reason muted">{E(r)}</p>' for r in auth.reasons)
                right = (f'<p class="verdict-line"><span class="verdict v-{a.status}">{E(a.status.upper())}</span> '
                         f'<span class="mono muted">{E(a.autonomy_level.value)}</span></p>{reasons}')
            else:
                auth = res.get("authorisation") or {}
                reasons = "".join(f'<p class="reason">{E(r)}</p>' for r in (auth.get("reasons") or [res.get("message","")]))
                right = f'<p class="verdict-line"><span class="verdict v-refused">REFUSED</span></p>{reasons}'
            out.append(f'<li class="rec"><div class="lane model">{left}</div><div class="rail"><span class="stamp"></span></div><div class="lane code">{right}</div></li>')
        elif tool == "load_skill":
            out.append(f'<li class="rec read"><div class="lane model"><p class="tool">{E(tool)} · {E(str(args.get("skill_name","")))}</p></div><div class="rail"><span class="stamp"></span></div><div class="lane code"><p class="muted">method guidance from the toolbox — context, never evidence</p></div></li>')
        else:
            out.append(f'<li class="rec read"><div class="lane model"><p class="tool">{E(tool)}</p></div><div class="rail"><span class="stamp"></span></div><div class="lane code"><p class="muted">read-only</p></div></li>')
    return "".join(out)


def sec_ledger(run, charter) -> str:
    return f'''
<section id="ledger" class="wide">
  {eyebrow("model", "the run, as it happened")}
  <h2>Left of the rail, the model. Right of the rail, code.</h2>
  <p>Every tool call from <code>trace.log</code>, in order, joined to the record it left in
  <code>ledger.jsonl</code>. The rail is the boundary: a hypothesis is written before its result is
  known, and nothing the model says can move a stamp.</p>
  <ol class="timeline">{timeline(run, charter)}</ol>
</section>'''


def sec_gate(charter) -> str:
    std = charter.evidence_standards
    available = gates.available()
    grows = "".join(
        f'<tr><td class="mono">{E(g["name"])}</td><td>{E(g["question_shape"])}</td><td class="mono">{E(", ".join(f"{k}: {v}" for k, v in g["spec_schema"].items()))}</td></tr>'
        for g in available)
    return f'''
<section id="gate">
  {eyebrow("code", "inside the gate")}
  <h2>Screen, test, decide. Three steps, none of them negotiable.</h2>
  <h3>1 · Screen the expression before anything runs</h3>
  <p>The <code>where</code> clause is parsed as a Python AST. Calls, attribute access, subscripts and
  comprehensions are refused outright, which is what keeps <code>df.eval</code> from becoming a way to
  run code. Then every column named is checked against the dataset, the standing identifier and
  protected-attribute screens, and the charter's forbidden and leakage lists.</p>
  {code_block(source_of(gates.screen), "py")}
  <h3>2 · Route to a gate</h3>
  <p>A gate is a shape of question, not a topic. These are the gates registered in this build:</p>
  <table><thead><tr><th>gate</th><th>question shape</th><th>spec</th></tr></thead><tbody>{grows}</tbody></table>
  <h3>3 · Decide against the charter's standard</h3>
  <p>This role's standard: at least <strong>{std.min_sample_size}</strong> rows, <strong>p &lt; {std.max_p_value}</strong>,
  effect at least <strong>{std.min_effect_size}×</strong>, corrected by <strong>{E(std.multiple_testing_correction)}</strong>
  across the whole run. INCONCLUSIVE is a first-class verdict: an underpowered test is not a false one.</p>
  {code_block(source_of(gates.decide), "py")}
  <p class="aside">Every failure becomes an Evidence record with a REFUSED verdict and a reason, never an
  exception. A refused idea is still a fact on record, so the next iteration does not re-derive it.</p>
</section>'''


def sec_challenge(run) -> str:
    ledger = run["ledger"]; objs = by_id(ledger)
    challenges = [e for e in ledger.all("evidence") if e.gate == "driver_effect"]
    rows = ""
    for e in challenges:
        h = objs[e.hypothesis_id]
        frac = e.statistics.get("confound_explains_fraction")
        rows += (f'<tr><td>{E(h.spec.get("where",""))}</td><td class="mono">{E(str(h.spec.get("control","")))}</td>'
                 f'<td>{verdict_badge(e.verdict.value)}</td><td class="mono">{E(str(e.statistics.get("adjusted_odds_ratio","—")))}</td>'
                 f'<td class="mono">{f"{frac:.0%}" if isinstance(frac,(int,float)) else "—"}</td></tr>')
    refusal = [ev for ev in run["trace"] if ev["event"] == "tool" and ev["tool"] == "record_finding" and (ev.get("result") or {}).get("status") == "refused"]
    example = ""
    if refusal:
        example = f'<h3>A refusal from this run</h3>{code_block(json.dumps(refusal[0]["result"], indent=2), "json")}'
    return f'''
<section id="challenge">
  {eyebrow("code", "the challenge · record_finding refuses an unchallenged subgroup")}
  <h2>A supported verdict is not yet a finding</h2>
  <p>SUPPORTED means the effect is unlikely to be noise. It does not mean the attribute the model named
  is the reason. So <code>record_finding</code> refuses any subgroup claim until the same subgroup has
  been re-tested with the <code>driver_effect</code> gate holding a plausible confound constant. The
  instruction to challenge is in the prompt too, but a prompt is not a mechanism; this is.</p>
  {code_block(source_of(tools.Toolbelt._unchallenged), "py")}
  <h3>Challenges this run actually ran</h3>
  <table><thead><tr><th>subgroup</th><th>control</th><th>verdict</th><th>adjusted OR</th><th>explained by confound</th></tr></thead><tbody>{rows or '<tr><td colspan="5">none</td></tr>'}</tbody></table>
  {example}
</section>'''


def sec_authority(charter, run) -> str:
    rows = "".join(
        f'<tr><td class="mono">{E(r.action_type)}</td><td>{E(r.description)}</td><td class="mono">{E(r.autonomy_level.value)}</td>'
        f'<td class="mono">{r.blast_radius.max_per_run} {E(r.blast_radius.unit)}</td><td class="mono">{E(r.observation_metric or "—")}</td><td>{E(r.requires_authority or "—")}</td></tr>'
        for r in charter.decision_rights)
    acts = run["ledger"].all("action")
    arows = "".join(
        f'<tr><td class="mono">{E(a.action_type)}</td><td><span class="verdict v-{a.status}">{E(a.status.upper())}</span></td><td class="mono">{E(a.autonomy_level.value)}</td><td class="mono">{E(json.dumps(a.blast_radius))}</td><td class="mono">{E(json.dumps(a.observation_plan))}</td></tr>'
        for a in acts)
    return f'''
<section id="authority">
  {eyebrow("code", "authority · authorise_action")}
  <h2>No action without a right, evidence for that right, a cap, and a way to observe it</h2>
  <p>The counterpart to the gate. The gate decides whether something is <em>true</em>; this decides
  whether the role may <em>act</em> on it. Four checks, all in fixed code: the charter names the action
  type; every cited finding clears the standard <em>for that type</em>; the blast radius is within cap;
  and there is a plan to measure the result. An action nobody measures can never be learned from, so
  it is not permitted.</p>
  <h3>What this charter grants</h3>
  <table><thead><tr><th>action type</th><th></th><th>autonomy</th><th>cap</th><th>observed by</th><th>needs</th></tr></thead><tbody>{rows}</tbody></table>
  <h3>Actions this run produced</h3>
  <table><thead><tr><th>type</th><th>status</th><th>autonomy</th><th>blast radius</th><th>observation plan</th></tr></thead><tbody>{arows or '<tr><td colspan="5">none</td></tr>'}</tbody></table>
  <p class="aside">Autonomy is granted by the charter per action type and capped at L1 (recommend) for any
  type with no observation metric, whatever the charter says. The model does not choose it.</p>
</section>'''


def sec_finalise(run) -> str:
    report = run["report"]
    log = "".join(f"<li class='mono'>{E(l)}</li>" for l in report["iteration_log"])
    return f'''
<section id="finalise">
  {eyebrow("code", "finalise · the whole run, corrected")}
  <h2>The loop stops on the ledger, and the run is corrected as a whole</h2>
  <p>The middleware caps iterations at the budget. Before that, <code>should_continue</code> reads the
  ledger, never the model's text: <strong>{runner.IDLE_PATIENCE}</strong> consecutive iterations that test
  nothing, or <strong>{runner.PATIENCE}</strong> that add no supported evidence, end the run after a minimum
  of <strong>{runner.MIN_ITERATIONS}</strong>. Then every p-value from the whole run is corrected together
  ({E(report.get("verdicts", {}) and "Benjamini–Hochberg")}), and a result that was only lucky is demoted
  before anyone reads it. <strong>{report["demoted_by_multiple_testing"]}</strong> were demoted in this run.</p>
  <ul class="log">{log}</ul>
  <details open><summary>report.json</summary>{code_block(json.dumps(report, indent=2), "json")}</details>
</section>'''


def sec_absent() -> str:
    return '''
<section id="absent">
  <p class="eyebrow"><span class="artefact">what is deliberately not here</span></p>
  <h2>Every mechanism above is one of three files</h2>
  <p>No shell, no sandbox, no filesystem policy, no workflow graph, no run registry, no fuzzy column
  matching. The certifying operations are three methods; only <code>gates.py</code> may construct an
  Evidence and a test fails the build otherwise. What the loop needs next is added when a run proves
  it is needed, and not before.</p>
</section>'''


# ------------------------------------------------------------------------------ page
CSS = r"""
:root{
  --paper:#EFF2F4; --paper-2:#F7F9FA; --ink:#15181D; --graphite:#59626E; --rule:#C7CED6;
  --code:#1F3A93; --model:#5B4B9E; --human:#8A5A00;
  --supported:#0B7A55; --rejected:#B42318; --inconclusive:#B26A00; --refused:#475467; --recorded:#0B7A55; --approved:#0B7A55; --proposed:#1F3A93;
  --display:"Bricolage Grotesque", "Avenir Next", "Helvetica Neue", system-ui, sans-serif;
  --body:"Source Serif 4", Georgia, "Times New Roman", serif;
  --mono:"IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
  --measure:70ch; --wide:1120px;
}
*{box-sizing:border-box} html{background:var(--paper);color:var(--ink);font:17px/1.55 var(--body);-webkit-text-size-adjust:100%}
body{margin:0;padding:0 24px 96px}
h1,h2,h3{font-family:var(--display);letter-spacing:-0.01em;line-height:1.1;margin:0 0 .6em}
h1{font-size:clamp(2.4rem,6vw,4.6rem);font-weight:800;letter-spacing:-0.03em}
h2{font-size:clamp(1.5rem,3vw,2.1rem);font-weight:700}
h3{font-size:1.05rem;font-weight:700;text-transform:none;margin-top:2.2rem;color:var(--graphite)}
p{margin:0 0 1em} code{font-family:var(--mono);font-size:.88em;background:var(--paper-2);padding:.05em .3em;border-radius:3px}
.mono{font-family:var(--mono);font-size:.86em} .muted{color:var(--graphite)}
.code-ink{color:var(--code)}
section, header.hero{max-width:var(--measure);margin:0 auto;padding:3.5rem 0 1rem;border-top:1px solid var(--rule)}
header.hero{border-top:0;padding-top:4.5rem}
section.wide{max-width:var(--wide)} section.wide > p, section.wide > h2, section.wide > .eyebrow{max-width:var(--measure)}
section.wide > pre.diagram{max-width:var(--wide)}
.kicker{font-family:var(--mono);font-size:.8rem;color:var(--graphite);letter-spacing:.02em}
.lede{font-size:1.15rem;max-width:62ch}
.eyebrow{display:flex;gap:.6rem;align-items:baseline;font-family:var(--mono);font-size:.78rem;letter-spacing:.04em;text-transform:uppercase;color:var(--graphite);margin-bottom:.9rem}
.who{padding:.1em .5em;border-radius:2px;color:#fff;font-weight:600}
.who-code{background:var(--code)} .who-model{background:var(--model)} .who-human{background:var(--human)}
.lanes,.timeline li.rec{display:grid;grid-template-columns:1fr 28px 1fr;gap:0 1.2rem;align-items:start}
.lane{min-width:0} .lane .lane-label{font-family:var(--mono);font-size:.74rem;text-transform:uppercase;letter-spacing:.05em;color:var(--graphite);margin-bottom:.35rem}
.lane.model .lane-label{color:var(--model)} .lane.code .lane-label{color:var(--code)}
.rail{position:relative;height:100%;min-height:2.2rem}
.rail::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:2px;background:var(--code);transform:translateX(-50%)}
.stamp{position:absolute;left:50%;top:.55rem;width:12px;height:12px;border-radius:50%;background:var(--paper);border:2px solid var(--code);transform:translateX(-50%)}
.hero-lanes{margin:2.2rem 0 1.6rem;padding:1.4rem 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.statement{font-size:1.02rem;margin:0 0 .35rem} .spec{color:var(--graphite);margin:0 0 .25rem;word-break:break-word}
.tool{font-family:var(--mono);font-size:.78rem;color:var(--model);margin:0 0 .3rem;letter-spacing:.02em}
.verdict{display:inline-block;font-family:var(--mono);font-size:.78rem;font-weight:700;letter-spacing:.06em;padding:.15em .55em;border-radius:2px;color:#fff;background:var(--refused)}
.v-supported,.v-recorded,.v-approved{background:var(--supported)} .v-rejected{background:var(--rejected)} .v-inconclusive{background:var(--inconclusive)} .v-refused{background:var(--refused)} .v-proposed{background:var(--proposed)}
.verdict-line{margin:.15rem 0 .35rem} .stats{margin:0 0 .2rem;color:var(--ink)} .reason{font-size:.92rem;margin:.2rem 0;color:var(--ink)}
.run-facts{display:flex;flex-wrap:wrap;gap:1.4rem 2.2rem;margin:0;padding:0}
.run-facts div{display:flex;flex-direction:column} .run-facts dt{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--graphite)} .run-facts dd{margin:0;font-size:1.6rem;font-family:var(--display);font-weight:700}
.timeline{list-style:none;padding:0;margin:1.5rem 0 0}
.timeline li.rec{padding:.9rem 0} .timeline li.rec .rail{align-self:stretch}
.timeline li.read{padding:.35rem 0;opacity:.72} .timeline li.read .tool{margin:0}
.timeline li.iter-start,.timeline li.iter-end{padding:.9rem 0 .9rem;margin:.6rem 0;border-top:2px solid var(--code);color:var(--graphite);font-size:.92rem}
.timeline li.iter-end{border-top:1px dashed var(--rule)}
.timeline li.iter-start .mono{color:var(--code);font-weight:700}
pre.code,pre.diagram{background:var(--paper-2);border:1px solid var(--rule);border-radius:4px;padding:1rem 1.1rem;overflow:auto;font-family:var(--mono);font-size:.8rem;line-height:1.5;margin:0 0 1.2rem}
pre.code{white-space:pre-wrap;word-break:break-word}
pre.diagram{font-size:.74rem;color:var(--ink)}
details{margin:0 0 1.2rem} summary{cursor:pointer;font-family:var(--mono);font-size:.82rem;color:var(--code);padding:.4rem 0}
table{width:100%;border-collapse:collapse;font-size:.92rem;margin:0 0 1.4rem} th{text-align:left;font-family:var(--mono);font-size:.74rem;text-transform:uppercase;letter-spacing:.05em;color:var(--graphite);padding:.4rem .5rem .4rem 0;border-bottom:1px solid var(--rule)}
td{padding:.5rem .5rem .5rem 0;border-bottom:1px solid var(--rule);vertical-align:top}
.aside{font-size:.95rem;color:var(--graphite);border-left:3px solid var(--rule);padding-left:1rem}
ol.questions{padding-left:1.2rem} ol.questions li{margin:0 0 1.2rem} ol.questions .qid{color:var(--graphite)} ol.questions p{margin:.2rem 0} ol.questions .why{color:var(--graphite);font-size:.92rem}
ul.log{list-style:none;padding:0} ul.log li{padding:.25rem 0;border-bottom:1px dashed var(--rule)}
nav.site{max-width:var(--measure);margin:0 auto;padding:1rem 0 0;font-family:var(--mono);font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;display:flex;flex-wrap:wrap;gap:.4rem 1.2rem}
nav.site a,nav.toc a{color:var(--code);text-decoration:none} nav.site a:hover,nav.site a:focus,nav.toc a:hover,nav.toc a:focus{text-decoration:underline}
nav.toc{max-width:var(--measure);margin:0 auto;padding:1rem 0 0;font-family:var(--mono);font-size:.8rem;display:flex;flex-wrap:wrap;gap:.4rem 1.2rem}
a:focus-visible,summary:focus-visible{outline:2px solid var(--code);outline-offset:2px}
footer{max-width:var(--measure);margin:3rem auto 0;font-family:var(--mono);font-size:.78rem;color:var(--graphite)}
@media (max-width:720px){ .lanes,.timeline li.rec{grid-template-columns:1fr} .rail{display:none} .lane.code{padding-left:.9rem;border-left:2px solid var(--code)} html{font-size:16px} }
"""


def build(run_dir: pathlib.Path, charter_path: pathlib.Path, data_path: pathlib.Path, out: pathlib.Path) -> None:
    charter = load_charter(charter_path)
    data = pd.read_csv(data_path); data.attrs["name"] = data_path.name
    run = load_run(run_dir)
    sections = [
        sec_hero(charter, run, data), sec_shape(), sec_charter(charter, charter_path),
        sec_data(charter, data, data_path), sec_questions(run), sec_context(run),
        sec_ledger(run, charter), sec_gate(charter), sec_challenge(run),
        sec_authority(charter, run), sec_finalise(run), sec_absent(),
    ]
    toc = "".join(f'<a href="#{i}">{t}</a>' for i, t in [
        ("shape", "the loop"), ("charter", "charter"), ("data", "data"), ("questions", "questions"),
        ("context", "an iteration's context"), ("ledger", "the run"), ("gate", "the gate"),
        ("challenge", "the challenge"), ("authority", "authority"), ("finalise", "finalise")])
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>strong-loop — the agent proposes, code disposes</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head>
<body>
<nav class="site" aria-label="customer journey"><a href="/">Overview</a><a href="/showcase.html">See it</a><a href="/loop.html" aria-current="page">Understand it</a><a href="/live.html">Try it live</a></nav>
<nav class="toc" aria-label="sections">{toc}</nav>
{"".join(sections)}
<footer>Generated {dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} from <span class="mono">{E(str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir))}</span> by tools/make_loop_doc.py. Regenerate after any change to the engine; the page has no hand-written numbers.</footer>
</body></html>'''
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({len(page)//1024} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=pathlib.Path)
    ap.add_argument("--charter", type=pathlib.Path, default=SERVICE / "charters" / "claims_analyst.yaml")
    ap.add_argument("--data", type=pathlib.Path, default=SERVICE / "data" / "claims.csv")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "docs" / "loop.html")
    a = ap.parse_args()
    build(a.run_dir.resolve(), a.charter, a.data, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
