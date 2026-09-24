/* charter-md.js — the standard Markdown charter format, parsed in the browser.
 *
 * A line-for-line port of src/strong-loop/loop/charter_md.py `parse`, so the charter
 * page can say "line 14: ..." as you type. The server parses again before anything is
 * signed — this copy only gives feedback. Both must pass tests/charter_md_cases.json
 * (tests/test_charter_md_js.py runs this file under node against the same cases).
 *
 * `check(raw)` adds the model's own rules the parser leaves to pydantic (closed
 * vocabularies, required fields), mirrored from loop/charter.py and loop/types.py.
 */
(function (root) {
  "use strict";

  class CharterFormatError extends Error {
    constructor(line, message) { super(`line ${line}: ${message}`); this.line = line; }
  }

  const FRONT = ["role", "version", "extends", "status", "ratified_by", "ratified_at", "content_hash"];
  const SECTIONS = ["glossary", "accountabilities", "decision rights", "evidence standard", "constraints"];
  const HEADING = /^(\S+)(?:\s+[—–-]{1,2}\s+(.*))?$/;
  const CAP = /^(\d+)(?:\s+(.+?))?\s+per\s+run$/i;
  const OBSERVE = /^(?:`?([^`\s]+)`?)?\s*(?:after\s+(\d+)\s+days?)?$/i;
  const DAYS = /^(\d+)(?:\s*days?)?$/i;
  const BOLD_TERM = /^\*\*(.+?)\*\*\s*:\s*(.*)$/;
  const ACC_KEYS = ["metric", "direction", "horizon", "priority", "lead"];
  const RIGHT_KEYS = ["autonomy", "reversible", "cap", "max value at risk", "observe", "approval"];
  const STANDARD_KEYS = {
    "min sample": ["min_sample_size", "int"], "max p": ["max_p_value", "float"],
    "min effect": ["min_effect_size", "float"], "correction": ["multiple_testing_correction", "str"],
    "requires causal design": ["requires_causal_design", "bool"],
    "requires holdout": ["requires_holdout", "bool"],
    "requires confound control": ["requires_confound_control", "bool"],
  };
  const CONSTRAINT_KEYS = {
    "never use": "forbidden_features", "leaks the outcome": "leakage_features",
    "only use": "allowed_features", "sensitive columns": "block_sensitive_columns", "notes": "notes",
  };

  // Python's repr() of a str, which the server's messages use.
  const repr = (s) => (s.includes("'") && !s.includes('"')) ? `"${s}"` : `'${s.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
  const text = (v) => v.split(/\s+/).filter(Boolean).join(" ");
  const ident = (v) => v.trim().replace(/^`+|`+$/g, "").trim();
  const idents = (v) => v.trim().toLowerCase() === "none" ? [] : v.split(",").map(ident).filter(Boolean);
  const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
  const allowed = (keys) => keys.map(k => "`" + k + "`").join(", ");
  const isSpace = (s) => !s.trim();

  function bool(n, key, value) {
    const v = value.trim().toLowerCase();
    if (["yes", "true", "y"].includes(v)) return true;
    if (["no", "false", "n"].includes(v)) return false;
    throw new CharterFormatError(n, `\`${key}\` must be yes or no, not ${repr(value)}`);
  }
  function number(n, key, value, kind) {
    const ok = kind === "int" ? /^\s*[+-]?\d+\s*$/.test(value) : /^\s*[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?\s*$/i.test(value);
    if (!ok) throw new CharterFormatError(n, `\`${key}\` must be a ${kind === "int" ? "whole number" : "number"}, not ${repr(value)}`);
    return kind === "int" ? parseInt(value, 10) : parseFloat(value);
  }
  function typed(n, key, value, kind) {
    if (kind === "bool") return bool(n, key, value);
    if (kind === "int" || kind === "float") return number(n, key, value, kind);
    return value.trim();
  }
  const partition = (s, sep) => { const i = s.indexOf(sep); return i < 0 ? [s, "", ""] : [s.slice(0, i), sep, s.slice(i + sep.length)]; };

  function parse(source) {
    const lines = source.split(/\r\n|\r|\n/);
    if (lines.length && lines[lines.length - 1] === "") lines.pop();   // splitlines() drops the final break
    const raw = {};
    let n = 0;
    while (n < lines.length && isSpace(lines[n])) n++;
    if (n >= lines.length || lines[n].trim() !== "---")
      throw new CharterFormatError(n + 1, "a charter starts with front matter: a `---` line, `role: <name>`, then another `---`");
    n++;
    for (;;) {
      if (n >= lines.length) throw new CharterFormatError(n, "front matter is never closed with `---`");
      const line = lines[n].trim(); n++;
      if (line === "---") break;
      if (!line) continue;
      let [key, sep, value] = partition(line, ":"); key = key.trim();
      if (!sep || !FRONT.includes(key)) throw new CharterFormatError(n, `front matter takes ${allowed(FRONT)} as \`key: value\`, not ${repr(line)}`);
      if (key in raw) throw new CharterFormatError(n, `\`${key}\` is given twice`);
      value = value.trim().replace(/^['"]+|['"]+$/g, "");
      raw[key] = key === "version" ? number(n, key, value, "int") : value;
    }
    if (!("role" in raw)) throw new CharterFormatError(n, "front matter must name the `role`");

    let section = null, block = null, blockKeys = new Set(), titled = false;
    const seen = new Set(), description = [];
    for (let i = n; i < lines.length; i++) {
      const ln = i + 1, s = lines[i].trim();
      if (!s || s.startsWith(">")) continue;
      if (s.startsWith("## ")) {
        const name = text(s.slice(3)).toLowerCase();
        if (!SECTIONS.includes(name)) throw new CharterFormatError(ln, `unknown section ${repr(s.slice(3).trim())}; sections are ` + SECTIONS.map(x => "`## " + capitalize(x) + "`").join(", "));
        if (seen.has(name)) throw new CharterFormatError(ln, `section \`## ${s.slice(3).trim()}\` appears twice`);
        seen.add(name); section = name; block = null; blockKeys = new Set();
        if (name === "evidence standard") block = raw.evidence_standards = raw.evidence_standards || {};
        else if (name === "constraints") block = raw.constraints = raw.constraints || {};
        continue;
      }
      if (s.startsWith("### ")) { block = item(ln, section, s.slice(4).trim(), raw); blockKeys = new Set(); continue; }
      if (s.startsWith("# ")) {
        if (section !== null || titled || description.length) throw new CharterFormatError(ln, "a `# title` belongs at the top, before the description");
        titled = true; continue;
      }
      if (s.startsWith("#")) throw new CharterFormatError(ln, `unexpected heading ${repr(s)}; use \`##\` sections and \`###\` items`);
      if (section === null) { description.push(s); continue; }
      if (!s.startsWith("- ")) throw new CharterFormatError(ln, `inside \`## ${capitalize(section)}\` every line is a \`- key: value\` bullet or a \`###\` item; start commentary with \`>\``);
      bullet(ln, section, s.slice(2).trim(), raw, block, blockKeys);
    }
    if (description.length) raw.description = text(description.join(" "));
    return raw;
  }

  function item(n, section, heading, raw) {
    if (section === "evidence standard") {
      const m = /^override\s*:\s*(\S+)$/i.exec(heading);
      if (!m) throw new CharterFormatError(n, "under `## Evidence standard` an item is `### Override: <action_type>`");
      const overrides = raw.evidence_overrides = raw.evidence_overrides || {};
      const name = ident(m[1]);
      if (name in overrides) throw new CharterFormatError(n, `override ${repr(name)} is given twice`);
      return overrides[name] = {};
    }
    const m = HEADING.exec(heading);
    let items, key, textKey;
    if (section === "accountabilities" && m) { items = raw.accountabilities = raw.accountabilities || []; key = "id"; textKey = "statement"; }
    else if (section === "decision rights" && m) { items = raw.decision_rights = raw.decision_rights || []; key = "action_type"; textKey = "description"; }
    else throw new CharterFormatError(n, "a `### <id> — <statement>` item belongs under `## Accountabilities` or `## Decision rights`");
    const name = ident(m[1]);
    if (items.some(x => x[key] === name)) throw new CharterFormatError(n, `${repr(name)} is defined twice`);
    const it = {[key]: name};
    if (m[2] && text(m[2])) it[textKey] = text(m[2]);
    items.push(it);
    return it;
  }

  function bullet(n, section, body, raw, block, blockKeys) {
    if (section === "glossary") {
      const m = BOLD_TERM.exec(body);
      let term, meaning;
      if (m) [term, meaning] = [m[1], m[2]]; else { const p = partition(body, ":"); [term, meaning] = [p[0], p[2]]; }
      term = text(term); meaning = text(meaning);
      if (!term || !meaning) throw new CharterFormatError(n, "a glossary line is `- **term**: what it means here`");
      const g = raw.glossary = raw.glossary || {};
      if (term in g) throw new CharterFormatError(n, `glossary term ${repr(term)} is defined twice`);
      g[term] = meaning; return;
    }
    let [key, sep, value] = partition(body, ":");
    key = text(key).toLowerCase(); value = value.trim();
    if (!sep || !value) throw new CharterFormatError(n, `expected \`- key: value\`, got ${repr(body)}`);
    if (block === null) throw new CharterFormatError(n, `start each ${section === "accountabilities" ? "accountability" : "decision right"} with a \`### <id> — <text>\` line before its bullets`);
    if (blockKeys.has(key) && key !== "lead") throw new CharterFormatError(n, `\`${key}\` is given twice`);
    blockKeys.add(key);

    if (section === "accountabilities") {
      if (key === "metric") block.metric = ident(value);
      else if (key === "direction") block.direction = value.toLowerCase();
      else if (key === "horizon") { const m = DAYS.exec(value); if (!m) throw new CharterFormatError(n, `\`horizon\` is a number of days, e.g. \`90 days\`, not ${repr(value)}`); block.horizon_days = parseInt(m[1], 10); }
      else if (key === "priority") block.priority = number(n, key, value, "int");
      else if (key === "lead") (block.leads = block.leads || []).push(text(value));
      else throw new CharterFormatError(n, `an accountability takes ${allowed(ACC_KEYS)}, not \`${key}\``);
    } else if (section === "decision rights") {
      if (key === "autonomy") block.autonomy_level = value;
      else if (key === "reversible") block.reversible = bool(n, key, value);
      else if (key === "cap") {
        const m = CAP.exec(value);
        if (!m) throw new CharterFormatError(n, `\`cap\` is \`<number> <unit> per run\`, e.g. \`200 claims per run\`, not ${repr(value)}`);
        const r = block.blast_radius = block.blast_radius || {}; r.max_per_run = parseInt(m[1], 10); if (m[2]) r.unit = text(m[2]);
      } else if (key === "max value at risk") (block.blast_radius = block.blast_radius || {}).max_value_at_risk = number(n, key, value, "float");
      else if (key === "observe") {
        const m = OBSERVE.exec(value);
        if (!m || !(m[1] || m[2])) throw new CharterFormatError(n, `\`observe\` is \`\` \`metric\` after <n> days \`\`, not ${repr(value)}`);
        if (m[1]) block.observation_metric = m[1];
        if (m[2]) block.observation_lag_days = parseInt(m[2], 10);
      } else if (key === "approval") block.requires_authority = ident(value);
      else throw new CharterFormatError(n, `a decision right takes ${allowed(RIGHT_KEYS)}, not \`${key}\``);
    } else if (section === "evidence standard") {
      if (!(key in STANDARD_KEYS)) throw new CharterFormatError(n, `an evidence standard takes ${allowed(Object.keys(STANDARD_KEYS))}, not \`${key}\``);
      const [field, kind] = STANDARD_KEYS[key]; block[field] = typed(n, key, value, kind);
    } else if (section === "constraints") {
      if (!(key in CONSTRAINT_KEYS)) throw new CharterFormatError(n, `constraints take ${allowed(Object.keys(CONSTRAINT_KEYS))}, not \`${key}\``);
      const field = CONSTRAINT_KEYS[key];
      if (field === "block_sensitive_columns") {
        const v = value.toLowerCase();
        if (v !== "blocked" && v !== "allowed") throw new CharterFormatError(n, `\`sensitive columns\` is \`blocked\` or \`allowed\`, not ${repr(value)}`);
        block[field] = v === "blocked";
      } else if (field === "notes") block[field] = text(value);
      else block[field] = idents(value);
    }
  }

  // ---- the model's rules the parser leaves to pydantic (loop/charter.py, loop/types.py)
  const AUTONOMY = ["L0_observe", "L1_recommend", "L2_act_reversible", "L3_act_report", "L4_act"];
  const DIRECTIONS = ["increase", "decrease", "stabilise"];
  const CORRECTIONS = ["benjamini_hochberg", "bonferroni", "none"];

  function check(raw) {
    const problems = [];
    const accs = raw.accountabilities || [];
    if (!accs.length && !raw.extends) problems.push("a charter needs at least one `### <id> — <statement>` under `## Accountabilities`");
    for (const a of accs) {
      if (!a.statement) problems.push(`accountability ${a.id}: write what it owns after the id — \`### ${a.id} — <statement>\``);
      if (!a.metric) problems.push(`accountability ${a.id}: needs \`- metric: \`<column>\`\``);
      else if (/\s/.test(a.metric.trim())) problems.push(`accountability ${a.id}: metric must be one column name, not ${repr(a.metric)}`);
      if (!a.direction) problems.push(`accountability ${a.id}: needs \`- direction:\` ${DIRECTIONS.join(", ")}`);
      else if (!DIRECTIONS.includes(a.direction)) problems.push(`accountability ${a.id}: direction must be ${DIRECTIONS.join(", ")}, not ${repr(a.direction)}`);
    }
    for (const r of raw.decision_rights || []) {
      if (r.autonomy_level && !AUTONOMY.includes(r.autonomy_level)) problems.push(`decision right ${r.action_type}: autonomy must be one of ${AUTONOMY.join(", ")}`);
    }
    const stds = [["evidence standard", raw.evidence_standards || {}], ...Object.entries(raw.evidence_overrides || {}).map(([k, v]) => [`override ${k}`, v])];
    for (const [where, s] of stds) {
      if (s.multiple_testing_correction && !CORRECTIONS.includes(s.multiple_testing_correction)) problems.push(`${where}: correction must be one of ${CORRECTIONS.join(", ")}`);
      if (s.max_p_value != null && !(s.max_p_value > 0 && s.max_p_value < 1)) problems.push(`${where}: max p must be between 0 and 1`);
    }
    const rights = new Set((raw.decision_rights || []).map(r => r.action_type));
    for (const k of Object.keys(raw.evidence_overrides || {})) if (!rights.has(k) && !raw.extends) problems.push(`override ${k}: no decision right has that action_type`);
    return problems;
  }

  const TEMPLATE = `---
role: my_role
version: 1
---
# My role
One or two sentences: what this role owns, and why it matters.

> Lines starting with ">" are notes for the author; the loop ignores them.

## Glossary
- **term**: what the word means in this business

## Accountabilities
> One per outcome the role owns. The metric must be a column in the data.
### first_goal — Reduce the thing this role is accountable for
- metric: \`column_name\`
- direction: decrease
- horizon: 90 days
- priority: 1
- lead: where you would look first (optional, repeatable)

## Decision rights
> What the role may do. Anything not listed here, it may not do.
### notify_team — Send a defined group to a named team for follow-up
- autonomy: L1_recommend
- reversible: yes
- cap: 100 records per run
- observe: \`column_name\` after 30 days

## Evidence standard
> The bar a result must clear before it can become a finding.
- min sample: 100
- max p: 0.05
- min effect: 1.2
- correction: benjamini_hochberg

## Constraints
- never use: \`customer_id\`
- sensitive columns: blocked
`;

  const api = {parse, check, CharterFormatError, TEMPLATE, AUTONOMY, DIRECTIONS, CORRECTIONS};
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.CharterMd = api;
})(typeof self !== "undefined" ? self : this);
