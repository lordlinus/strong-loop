/* wheel.js — the loop on one ring, driven by events.
 *
 *   const w = Wheel.mount(host, {onOpen: key => ..., lookup: {...}});
 *   w.setInput(summary); w.reset(); w.apply(event);   // event from Wheel.fromTrace or Wheel.adapter()
 *
 * One `apply` for a recorded replay (index.html) and the live Responses stream
 * (live.html), so the two can never show different loops. Everything shown is read off
 * the event; `lookup` (a recorded run's ledger) only adds detail when present.
 * `Wheel.narrate(event)` is the caption: one plain sentence saying who did what.
 */
(function (root) {
  "use strict";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const trunc = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
  const fmtP = (p) => p == null ? "—" : (p < 0.001 ? Number(p).toExponential(1) : Number(p).toFixed(3));
  const badge = (v) => `<span class="verdict v-${esc(String(v).toLowerCase())}">${esc(String(v).toUpperCase())}</span>`;
  const parse = (s) => { if (s && typeof s === "object") return s; try { return JSON.parse(s); } catch { return null; } };
  const WRITE_TOOLS = new Set(["test_hypothesis", "record_finding", "propose_action"]);
  const WRITES = new Set(["test", "challenge", "record", "act"]);

  // Clockwise from 12. `role`/`say` are the model's side (outside the ring), `mech` is
  // what code does at that station (inside). Plain words on the face; detail on click.
  const STATIONS = [
    {id: "boundary", name: "continue?", mech: "code reads the ledger · go on or stop", role: "starts every round from nothing",
     say: `<em>Gets the rules, your steer and a summary of the ledger. It remembers nothing else.</em>`},
    {id: "read", name: "read", mech: "read-only · nothing is written", role: "reads its brief",
     say: `Its questions come from the charter. <em>It picks one; it cannot invent one.</em>`},
    {id: "test", name: "test", mech: "code runs the statistics", role: "commits to a claim",
     say: `Written down before any result is known. <em>No re-wording afterwards.</em>`},
    {id: "challenge", name: "challenge", mech: "code re-tests, confound held fixed", role: "tries to break it",
     say: `Same group, with a likely alternative explanation held constant.`},
    {id: "record", name: "record", mech: "code sets confidence · may refuse", role: "writes up a finding",
     say: `Must cite the evidence. <em>Confidence comes from the statistics, not the model.</em>`},
    {id: "act", name: "act", mech: "code checks the right, the cap, the proof", role: "proposes an action",
     say: `Who, how many, and how it will be checked. <em>Only what the charter grants.</em>`},
  ];
  const TOOL_WORDS = {get_progress: "the ledger so far", list_questions: "its open questions", get_charter: "the charter",
    list_gates: "the tests it may run", get_data_profile: "a profile of the data", load_skill: "method guidance", web: "the web"};

  function fromTrace(ev) {
    if (ev.event === "iteration_start") return {kind: "iteration_start", iteration: ev.iteration, max_iterations: ev.max_iterations, summary: ev.summary};
    if (ev.event === "iteration_end") return {kind: "iteration_end", ...ev};
    if (ev.event === "report") return {kind: "report", report: ev.report};
    return {kind: "tool", tool: ev.tool, args: ev.args || {}, result: parse(ev.result) || {status: "read"}};
  }

  /** Responses stream items -> wheel events. One per stream; it pairs calls with outputs. */
  function adapter() {
    const calls = new Map();
    return function (item) {
      if (!item) return null;
      if (item.type === "function_call") { calls.set(item.call_id, {name: item.name || "?", args: parse(item.arguments) || {}}); return null; }
      if (item.type !== "function_call_output") return null;
      const c = calls.get(item.call_id); if (!c) return null;
      const out = parse(item.output);
      if (c.name === "loop.iteration_start") return {kind: "iteration_start", iteration: c.args.iteration, max_iterations: c.args.max_iterations, summary: (out || {}).ledger_summary};
      if (c.name === "loop.iteration_end") return {kind: "iteration_end", iteration: c.args.iteration, ...(out || {})};
      if (c.name === "loop.report") return {kind: "report", report: out || {}};
      if (c.name.startsWith("loop.")) return null;
      return {kind: "tool", tool: c.name, args: c.args, result: out || {status: "read"}};
    };
  }

  function tally(ev) {
    const m = /tested (\d+), newly supported (\d+)/.exec(ev.line || "");
    return {tested: ev.tested ?? (m && m[1]) ?? "?", sup: ev.newly_supported ?? (m && m[2]) ?? "?"};
  }

  /** One sentence per event: {who, text, tone}. `who` is model | code | human. */
  function narrate(ev, lookup) {
    if (!ev) return null;
    if (ev.kind === "iteration_start") return {who: "code", tone: "", text: `Round ${ev.iteration} of ${ev.max_iterations} begins from an empty context. The model knows only the rules, your steer and the ledger summary.`};
    if (ev.kind === "iteration_end") { const t = tally(ev); return {who: "code", tone: "", text: `Round ${ev.iteration} ends: ${t.tested} tested, ${t.sup} newly supported. Code reads the ledger — not the model's opinion — to decide whether to go on.`}; }
    if (ev.kind === "report") {
      const r = ev.report || {}; const acts = (r.actions || []).filter(a => a.status === "approved" || a.status === "proposed").length;
      return {who: "code", tone: "supported", text: `Finished. All ${r.hypotheses_tested ?? "the"} results were corrected together for multiple testing (${r.demoted_by_multiple_testing ?? 0} demoted). ${(r.findings || []).length} findings, ${acts} authorised action${acts === 1 ? "" : "s"}.`};
    }
    const {tool, args = {}} = ev; const res = ev.result || {};
    if (!WRITE_TOOLS.has(tool)) return {who: "model", tone: "", text: tool === "load_skill" ? `The model loads method guidance (${args.skill_name || "a skill"}). It is context, never evidence.` : `The model reads ${TOOL_WORDS[tool] || tool}. Nothing is written.`};
    if (tool === "test_hypothesis") {
      const claim = `“${trunc(args.statement || "", 140)}”`;
      if (res.status !== "tested") return {who: "code", tone: "refused", text: `The model proposed ${claim}. Code would not run it: ${trunc(res.message || res.status, 180)}`};
      if (res.verdict === "REFUSED") return {who: "code", tone: "refused", text: `The model proposed ${claim}. Code refused to test it: ${trunc(res.refusal_reason || "it breaks a constraint", 200)}`};
      const s = res.statistics || {};
      if (args.kind === "driver_effect") {
        const f = s.confound_explains_fraction;
        return {who: "code", tone: res.verdict === "SUPPORTED" ? "supported" : "", text: `The model tried to break its own result by holding ${s.control || (args.spec || {}).control || "a confound"} fixed. Code: ${res.verdict}${typeof f === "number" ? `, the confound explains ${Math.round(f * 100)}% of it` : ""}.`};
      }
      const words = {SUPPORTED: "the data supports it", REJECTED: "the data does not support it", INCONCLUSIVE: "too little data to say either way — not the same as false"};
      return {who: "code", tone: res.verdict === "SUPPORTED" ? "supported" : "", text: `The model claimed ${claim}. Code tested it on the rows: ${res.verdict} — ${words[res.verdict] || ""} (effect ${Number(res.effect_size ?? 0).toFixed(2)}×, p ${fmtP(res.p_value)}, n ${Number(res.sample_size ?? 0).toLocaleString()}).`};
    }
    if (tool === "record_finding") {
      if (res.status === "recorded") {
        const f = lookup && lookup.finding ? lookup.finding(res.finding_id) : null;
        const warn = (res.warnings || []).length ? ` It carries a warning: ${res.warnings.join("; ")}.` : "";
        return {who: "code", tone: warn ? "warn" : "supported", text: `A finding is recorded: “${trunc(args.headline || (f && f.headline) || "", 140)}”. Its confidence (${Number(res.confidence).toFixed(2)}) was computed from the evidence, not claimed by the model.${warn}`};
      }
      return {who: "code", tone: "refused", text: `Code refused to record the finding: ${trunc(res.message || res.status, 220)}`};
    }
    if (tool === "propose_action") {
      const st = res.status || "refused"; const rec = trunc(args.recommendation || args.action_type || "", 120);
      if (st === "refused") return {who: "code", tone: "refused", text: `The model proposed “${rec}”. Code refused: ${trunc(res.message || ((res.authorisation || {}).reasons || []).join("; "), 200)}`};
      const who = st === "approved" ? "the charter lets it proceed" : "a named person must sign it off first";
      return {who: "code", tone: "supported", text: `The model proposed “${rec}” for ${Number(res.target_rows ?? 0).toLocaleString()} rows. Code checked the right, the cap and the evidence: ${st.toUpperCase()} — ${who}.`};
    }
    return null;
  }

  function mount(host, opts = {}) {
    const lookup = opts.lookup || {};
    host.classList.add("wheel");
    host.innerHTML = `<svg class="orbit" aria-hidden="true"></svg><div class="stations"></div>
      <button class="ledger" data-open="ledger" aria-label="open the ledger"><h2>LEDGER</h2><p class="sub">append-only · the only memory</p>
      <dl><div><dt>tested</dt><dd data-c="tested">0</dd></div><div><dt>supported</dt><dd data-c="sup">0</dd></div>
      <div><dt>findings</dt><dd data-c="find">0</dd></div><div><dt>actions</dt><dd data-c="act">0</dd></div></dl>
      <p class="foot"><span>round <b data-c="iter">—</b></span><span>demoted <b data-c="dem">—</b></span></p></button>
      <div class="side l"><button class="card human" data-open="input"></button></div>
      <div class="side r"><button class="card code dim" data-open="output"></button></div>`;
    const q = (sel) => host.querySelector(sel);
    const st = (id, part) => host.querySelector(`[data-s="${id}-${part}"]`);
    const stations = q(".stations");
    STATIONS.forEach((s, i) => {
      const anchor = i === 0 || i === 3 ? "center" : (i < 3 ? "left" : "right");
      stations.insertAdjacentHTML("beforeend",
        `<button class="outside ${anchor}" data-s="${s.id}-out" data-open="${s.id}"><p class="role">${esc(s.role)}</p><p class="say">${s.say}</p><p class="quote" data-s="${s.id}-quote"></p></button>` +
        `<div class="inside" data-s="${s.id}-in"><p class="mech">${esc(s.mech)}</p><p class="val" data-s="${s.id}-val">—</p></div>` +
        `<button class="pill" data-s="${s.id}-pill" data-open="${s.id}">${esc(s.name)}</button>`);
    });
    host.addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b && opts.onOpen) opts.onOpen(b.dataset.open); });

    const state = {tested: 0, sup: 0, find: 0, act: 0, iter: "—", max: "—", dem: "—"};
    let active = null;
    const counts = () => { for (const k of ["tested", "sup", "find", "act", "dem"]) q(`[data-c="${k}"]`).textContent = state[k]; q('[data-c="iter"]').textContent = state.iter === "—" ? "—" : `${state.iter} / ${state.max}`; };
    const setActive = (id) => { if (active) active.classList.remove("active"); active = id ? st(id, "pill") : null; if (active) active.classList.add("active"); };
    const show = (id, {val, quote, refused}) => {
      if (val != null) st(id, "val").innerHTML = val;
      if (quote != null) st(id, "quote").textContent = quote;
      st(id, "in").classList.toggle("refused", !!refused);
    };

    function setInput(s) {
      s = s || {};
      const arrow = (d) => d === "decrease" ? "↓" : d === "increase" ? "↑" : d === "stabilise" ? "=" : "";
      const acc = (s.accountabilities || []).map(a => `${esc(a.metric)} ${arrow(a.direction)}`).join(" · ");
      const rights = (s.rights || []).map(r => `${esc(r.action_type)} <b>${esc(String(r.autonomy_level || "").split("_")[0])}</b> ≤${r.max_per_run}`).join(" · ");
      const std = s.standard;
      q(".card.human").innerHTML = `<p class="eyebrow"><span class="io">input</span><span class="who who-human">human</span><span>signs the charter</span></p>
        <h3>${esc(s.role || "a role charter")}</h3>
        ${acc ? `<p class="std human">accountable for <b>${acc}</b></p>` : ""}
        ${rights ? `<p class="std human">may ${rights}</p>` : ""}
        ${std ? `<p class="std human">proof: n ≥ <b>${std.min_sample_size}</b> · p &lt; <b>${std.max_p_value}</b> · effect ≥ <b>${std.min_effect_size}×</b></p>` : ""}
        ${s.dataset ? `<p class="std muted">${esc(s.dataset.name)} · ${Number(s.dataset.rows || 0).toLocaleString()} rows${s.questions != null ? ` → ${s.questions} questions, compiled before any model call` : ""}</p>` : ""}`;
    }
    function setOutput(r) {
      const card = q(".card.code");
      card.classList.toggle("dim", !r);
      if (!r) { card.innerHTML = `<p class="eyebrow"><span class="io">output</span><span class="who who-code">code</span><span>report</span></p><h3>written when the run ends</h3><p class="muted" style="font-size:.95em">findings, actions, every p-value corrected together</p>`; return; }
      const finds = (r.findings || []).map(f => `<li><span class="k">${Number(f.confidence).toFixed(2)}</span><span class="t">${esc(f.headline)}</span></li>`).join("");
      const acts = (r.actions || []).map(a => `<li><span class="k">${badge(a.status)}</span><span class="t">${esc(a.type)} · ${esc(a.autonomy)}</span></li>`).join("");
      const n = (r.actions || []).filter(a => a.status === "approved" || a.status === "proposed").length;
      card.innerHTML = `<p class="eyebrow"><span class="io">output</span><span class="who who-code">code</span><span>report</span></p>
        <h3>${(r.findings || []).length} findings · ${n} action${n === 1 ? "" : "s"}</h3>
        <ul>${finds || "<li class='muted'>no findings</li>"}</ul><ul style="margin-top:.3em">${acts || "<li class='muted'>no actions</li>"}</ul>
        <p class="std muted">${r.hypotheses_tested ?? 0} results corrected together · ${r.demoted_by_multiple_testing ?? 0} demoted</p>`;
    }
    function reset() {
      Object.assign(state, {tested: 0, sup: 0, find: 0, act: 0, iter: "—", max: "—", dem: "—"}); counts(); setActive(null);
      STATIONS.forEach(s => show(s.id, {val: "—", quote: ""})); setOutput(null);
    }

    function apply(ev) {
      if (!ev) return;
      if (ev.kind === "iteration_start") {
        state.iter = ev.iteration; state.max = ev.max_iterations; counts(); setActive("boundary");
        const s = ev.summary || {};
        show("boundary", {val: `${badge("round " + ev.iteration + "/" + ev.max_iterations)} <span>empty context</span>`,
          quote: `handed a ledger summary: ${s.hypotheses_tested ?? state.tested} tested, ${(s.findings || []).length} findings`});
        return;
      }
      if (ev.kind === "iteration_end") {
        setActive("boundary"); const t = tally(ev);
        show("boundary", {val: `${badge("round " + (ev.iteration ?? state.iter) + "/" + state.max)} <span>${esc(t.tested)} tested · ${esc(t.sup)} supported</span>`,
          quote: `code read the ledger · no progress ${ev.stagnant ?? 0}, idle ${ev.idle ?? 0}`});
        return;
      }
      if (ev.kind === "report") { setActive(null); const r = ev.report || {}; state.dem = r.demoted_by_multiple_testing ?? 0; counts(); setOutput(r); return; }

      const {tool, args = {}} = ev; const res = ev.result || {};
      if (!WRITE_TOOLS.has(tool)) {
        setActive("read");
        show("read", {val: `${badge(tool === "load_skill" ? "skill" : "read")} <span>${esc(args.skill_name || TOOL_WORDS[tool] || tool)}</span>`, quote: ""});
        return;
      }
      if (tool === "test_hypothesis") {
        const id = args.kind === "driver_effect" ? "challenge" : "test"; setActive(id);
        const quote = `“${args.statement || ""}”`;
        if (res.status !== "tested") { show(id, {val: `${badge(res.status || "refused")} <span>${esc(trunc(res.message || "", 28))}</span>`, quote, refused: true}); return; }
        state.tested++; if (res.verdict === "SUPPORTED") state.sup++; counts();
        const e = lookup.evidence ? lookup.evidence(res.evidence_id) : null; const s = res.statistics || (e && e.statistics) || {};
        let val;
        if (res.verdict === "REFUSED") val = `${badge("refused")} <span>${esc(trunc(res.refusal_reason || "constraint", 26))}</span>`;
        else if (id === "challenge") { const f = s.confound_explains_fraction; val = `${badge(res.verdict)} <span>${esc(s.control || (args.spec || {}).control || "confound")} · ${typeof f === "number" ? Math.round(f * 100) + "%" : "?"}</span>`; }
        else val = `${badge(res.verdict)} <span>${Number(res.effect_size ?? 0).toFixed(2)}× · n ${Number(res.sample_size ?? 0).toLocaleString()}</span>`;
        show(id, {val, quote, refused: res.verdict === "REFUSED"});
        return;
      }
      if (tool === "record_finding") {
        setActive("record");
        if (res.status === "recorded") {
          state.find++; counts(); const f = lookup.finding ? lookup.finding(res.finding_id) : null;
          show("record", {val: `${badge("recorded")} <span>confidence ${Number(res.confidence).toFixed(2)}</span>`, quote: `“${args.headline || (f && f.headline) || ""}”`});
        } else show("record", {val: `${badge(res.status || "refused")} <span>${esc(trunc(res.message || "", 26))}</span>`, quote: trunc(res.message || "", 140), refused: true});
        return;
      }
      if (tool === "propose_action") {
        setActive("act"); const s = res.status || "refused"; if (s === "approved" || s === "proposed") state.act++; counts();
        show("act", {val: `${badge(s)} <span>${esc((res.autonomy_level || args.action_type || "").replace(/_/g, " "))}</span>`,
          quote: `“${args.recommendation || args.action_type || ""}”`, refused: s === "refused"});
      }
    }

    function layout() {
      const box = host.getBoundingClientRect(); const W = box.width, H = box.height;
      if (!W || !H) return;
      const R = Math.min(0.68 * H, 0.42 * W) / 2, cx = W / 2, cy = H / 2, pad = 30;
      const em = parseFloat(getComputedStyle(host).fontSize);
      host.style.setProperty("--chip-w", Math.round(Math.max(7.5 * em, Math.min(10 * em, R * 0.52))) + "px");
      const ledger = q(".ledger"); let ledgerPx = Math.max(10, Math.min(0.9 * em, R / 300 * 0.9 * em)); ledger.style.fontSize = ledgerPx.toFixed(1) + "px";
      const svg = q(".orbit");
      svg.style.left = (cx - R - pad) + "px"; svg.style.top = (cy - R - pad) + "px"; svg.style.width = svg.style.height = (2 * R + 2 * pad) + "px";
      svg.setAttribute("viewBox", `${-R - pad} ${-R - pad} ${2 * R + 2 * pad} ${2 * R + 2 * pad}`);
      const card = ledger.getBoundingClientRect(); const cardR = Math.hypot(card.width, card.height) / 2 * 0.86;
      let g = `<defs><marker id="wa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#1F3A93"/></marker><marker id="wh" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8A5A00"/></marker></defs>`;
      g += `<circle class="ring" cx="0" cy="0" r="${R}"/>`;
      STATIONS.forEach((s, i) => {
        const th = i * Math.PI / 3, sx = R * Math.sin(th), sy = -R * Math.cos(th), ex = cardR * Math.sin(th), ey = -cardR * Math.cos(th);
        if (s.id === "boundary") g += `<line class="spoke out" x1="${ex}" y1="${ey}" x2="${sx}" y2="${sy + 16}" marker-end="url(#wa)"/>`;
        else if (WRITES.has(s.id)) g += `<line class="spoke" x1="${sx}" y1="${sy}" x2="${ex}" y2="${ey}" marker-end="url(#wa)"/>`;
        else g += `<line class="spoke read" x1="${sx}" y1="${sy}" x2="${ex}" y2="${ey}"/>`;
        const tm = th + Math.PI / 6;
        g += `<path class="chev" d="M-7,-6 L3,0 L-7,6 z" transform="translate(${R * Math.sin(tm)},${-R * Math.cos(tm)}) rotate(${tm * 180 / Math.PI})"/>`;
      });
      const hc = q(".card.human").getBoundingClientRect(), fc = q(".card.code").getBoundingClientRect();
      const inX = hc.right - box.left - cx + 8, outX = fc.left - box.left - cx - 8, fs = (0.62 * em).toFixed(1);
      g += `<line class="io in" x1="${inX}" y1="0" x2="${-R - 12}" y2="0" marker-end="url(#wh)"/><text class="io-t in" font-size="${fs}" x="${(inX - R - 12) / 2}" y="-9" text-anchor="middle">charter → questions</text>`;
      g += `<line class="io out" x1="${R + 12}" y1="0" x2="${outX}" y2="0" marker-end="url(#wa)"/><text class="io-t out" font-size="${fs}" x="${(R + 12 + outX) / 2}" y="-9" text-anchor="middle">report</text>`;
      svg.innerHTML = g;
      STATIONS.forEach((s, i) => {
        const th = i * Math.PI / 3, x = cx + R * Math.sin(th), y = cy - R * Math.cos(th);
        const pill = st(s.id, "pill"); pill.style.left = x + "px"; pill.style.top = y + "px";
        const chip = st(s.id, "in"); const a = chip.offsetWidth / 2, b = chip.offsetHeight / 2;
        let r = R; for (; r > 0; r -= 2) { if (Math.hypot(r * Math.abs(Math.sin(th)) + a, r * Math.abs(Math.cos(th)) + b) <= R - 14) break; }
        chip.style.left = (cx + r * Math.sin(th)) + "px"; chip.style.top = (cy - r * Math.cos(th)) + "px";
        const out = st(s.id, "out"); const w = out.offsetWidth, h = out.offsetHeight, gx = pill.offsetWidth / 2 + 8;
        if (i === 0) { out.style.left = (cx - w / 2) + "px"; out.style.top = (y - 18 - h) + "px"; }
        else if (i === 3) { out.style.left = (cx - w / 2) + "px"; out.style.top = (y + 18) + "px"; }
        else if (i === 1) { out.style.left = (x + gx) + "px"; out.style.top = (y + 12 - h) + "px"; }
        else if (i === 2) { out.style.left = (x + gx) + "px"; out.style.top = (y - 12) + "px"; }
        else if (i === 4) { out.style.left = (x - gx - w) + "px"; out.style.top = (y - 12) + "px"; }
        else { out.style.left = (x - gx - w) + "px"; out.style.top = (y + 12 - h) + "px"; }
      });
      for (let k = 0; k < 6; k++) {
        const L = ledger.getBoundingClientRect();
        const hit = [...host.querySelectorAll(".inside")].some(c => { const r = c.getBoundingClientRect(); return r.left < L.right - 1 && L.left < r.right - 1 && r.top < L.bottom - 1 && L.top < r.bottom - 1; });
        if (!hit) break; ledgerPx -= 0.5; ledger.style.fontSize = ledgerPx.toFixed(1) + "px";
      }
    }

    reset(); setInput(opts.input); requestAnimationFrame(layout);
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => layout()).observe(host);
    return {apply, reset, layout, setInput, setOutput, state};
  }

  /** Render a narration into a caption element. */
  function caption(el, n) {
    if (!el) return;
    el.className = "wheel-caption" + (n && n.tone ? " " + n.tone : "");
    el.innerHTML = n ? `<span class="who who-${esc(n.who)}">${esc(n.who)}</span>${esc(n.text)}` : "";
  }

  const api = {mount, adapter, fromTrace, narrate, caption, STATIONS, esc, badge, fmtP};
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.Wheel = api;
})(typeof self !== "undefined" ? self : this);
