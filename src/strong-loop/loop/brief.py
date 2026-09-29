"""brief.py — what the persona holding this charter does, in one place.

The charter says it; this module only arranges it, with THIS data's facts beside it: the
level of every metric today, the columns this role may never put in a rule, the proof the
gate will ask for. Built by code from the charter and the pairing, never by a model, so the
person pressing start and the model doing the work read the same thing.

Three readers:
  - the intake report (`IntakeReport.brief`), which the live page shows before start
  - every fresh iteration, as instructions (`render`), so the model knows who it is
    without spending a tool call to find out
  - the report, which opens with it and closes with `coverage`: what each accountability
    got out of the run

No domain vocabulary. Every word here comes from the charter or the data.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import gates
from .charter import RoleCharter
from .derived import Derived

_VERB = {"increase": "raise", "decrease": "lower", "stabilise": "stabilise"}

# What each autonomy level lets the role do with a play, in words a person acts on.
_AUTONOMY = {
    "L0_observe": "observe only; no action",
    "L1_recommend": "recommends; a person decides and acts",
    "L2_act_reversible": "acts itself, reversibly",
    "L3_act_report": "acts itself, then reports",
    "L4_act": "acts itself",
}

_MAX_LISTED = 12   # columns named per list; the gate refuses the rest either way


def _words(text: str) -> str:
    return " ".join(str(text or "").split())


def _num(x: float) -> str:
    if abs(x) >= 100:
        return f"{x:,.0f}"
    return f"{x:,.2f}".rstrip("0").rstrip(".") if abs(x) >= 1 else f"{x:.3g}"


def _today(measure: dict[str, Any]) -> str:
    if measure.get("kind") == "rate":
        return f"rate {measure['base_rate'] * 100:.1f}% ({measure['positives']:,} of {measure['n']:,})"
    if measure.get("kind") == "numeric":
        return f"mean {_num(measure['mean'])} · median {_num(measure['median'])} (n {measure['n']:,})"
    return ""


def _sentence(text: str) -> str:
    text = _words(text)
    return text if not text or text[-1] in ".!?" else text + "."


def _bar(standard) -> dict[str, Any]:
    return {
        "min_sample_size": standard.min_sample_size,
        "max_p_value": standard.max_p_value,
        "min_effect_size": standard.min_effect_size,
        "correction": standard.multiple_testing_correction,
        "requires_causal_design": standard.requires_causal_design,
        "requires_confound_control": standard.requires_confound_control,
    }


def role_brief(charter: RoleCharter, data: pd.DataFrame | None = None, derived: Derived | None = None,
               context: dict[str, Any] | None = None, measures: dict[str, Any] | None = None) -> dict[str, Any]:
    """The persona, as a structured record. `measures` may be passed when the caller has
    already profiled the data (`intake.pair` has)."""
    context = context or {}
    if measures is None and data is not None:
        from .tools import profile
        measures = profile(data, charter, derived)["accountability_measures"]
    measures = measures or {}
    withheld = {**(derived.unmeasurable if derived else {}), **(derived.withheld if derived else {})}

    objectives = []
    for acc in sorted(charter.accountabilities, key=lambda a: a.priority):
        present = data is None or acc.metric in data.columns
        objectives.append({
            "id": acc.id,
            "statement": _sentence(acc.statement),
            "metric": acc.metric,
            "direction": acc.direction,
            "goal": f"{_VERB[acc.direction]} {acc.metric}",
            "horizon_days": acc.horizon_days,
            "priority": acc.priority,
            "today": _today(measures.get(acc.metric, {})),
            "leads": list(acc.leads),
            "status": ("withheld" if acc.metric in withheld else "open") if present else "missing",
            **({"why_not": withheld[acc.metric]} if acc.metric in withheld else {}),
        })

    plays = []
    for right in charter.decision_rights:
        override = charter.evidence_overrides.get(right.action_type)
        plays.append({
            "action_type": right.action_type,
            "description": _sentence(right.description),
            "autonomy": right.autonomy_level.value,
            "who_acts": _AUTONOMY.get(right.autonomy_level.value, right.autonomy_level.value),
            "reversible": right.reversible,
            "cap": f"at most {right.blast_radius.max_per_run:,} {right.blast_radius.unit} per run",
            "signs_off": right.requires_authority,
            "observe": ({"metric": right.observation_metric, "lag_days": right.observation_lag_days}
                        if right.observation_metric else {}),
            **({"stricter_bar": _bar(override)} if override else {}),
        })

    never: list[dict[str, str]] = []
    seen: set[str] = set()

    def ban(column: str, reason: str) -> None:
        if column not in seen:
            seen.add(column)
            never.append({"column": column, "reason": reason})

    c = charter.constraints
    for col in c.forbidden_features:
        ban(col, "forbidden by the charter")
    for col in c.leakage_features:
        ban(col, "leaks the outcome (charter)")
    if data is not None:
        if c.block_sensitive_columns:
            for col in data.columns:
                if gates.is_sensitive_column(col):
                    ban(col, "identifier, personal data or protected attribute")
        for col, reason in ((derived.identifiers if derived else {}) | (derived.protected if derived else {})).items():
            ban(col, reason)

    not_a_driver_of: dict[str, list[str]] = {}
    for col, metrics in sorted((derived.leaks if derived else {}).items()):
        for m in metrics:
            not_a_driver_of.setdefault(m, []).append(col)

    return {
        "role": charter.role,
        "version": charter.version,
        "status": charter.status,
        "ratified_by": charter.ratified_by,
        "mission": _words(charter.description),
        "objectives": objectives,
        "plays": plays,
        "never": never,
        "not_a_driver_of": not_a_driver_of,
        "evidence_bar": _bar(charter.evidence_standards),
        "glossary_in_data": dict(context.get("bindings") or {}),
        "notes": list(context.get("notes") or []),
    }


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    return shown + (f" (+{len(items) - _MAX_LISTED} more)" if len(items) > _MAX_LISTED else "")


def _bar_text(bar: dict[str, Any]) -> str:
    text = (f"n ≥ {bar['min_sample_size']}, p ≤ {bar['max_p_value']}, effect ≥ {bar['min_effect_size']}×, "
            f"{bar['correction']} across the run")
    if bar.get("requires_causal_design"):
        text += ", causal design required"
    return text


def render(brief: dict[str, Any]) -> str:
    """The brief as the model reads it at the top of every iteration. Kept terse: it
    competes for the same context as the ledger summary and the work itself."""
    lines = [f"YOUR ROLE: {brief['role']} (v{brief['version']}, {brief['status']})"]
    if brief["mission"]:
        lines.append(brief["mission"])

    lines += ["", "WHAT YOU ARE ACCOUNTABLE FOR, in priority order (each is one open question):"]
    n = 0
    off: list[str] = []
    for o in brief["objectives"]:
        if o["status"] != "open":
            off.append(f"{o['id']} ({o['metric']}): {o.get('why_not') or o['status']}")
            continue
        n += 1
        today = f" — today {o['today']}" if o["today"] else ""
        lines.append(f"{n}. [{o['id']}] {o['goal']} within {o['horizon_days']} days{today}. {o['statement']}")
        if o["leads"]:
            lines.append(f"   Look first at: {' | '.join(o['leads'])}")
    if off:
        lines.append("Not covered this run: " + "; ".join(off))

    if brief["plays"]:
        lines += ["", "WHAT YOU MAY DO — only on a recorded finding, only to the group you tested:"]
        for p in brief["plays"]:
            extra = f"; {p['signs_off']} signs off" if p["signs_off"] else ""
            stricter = f"; needs {_bar_text(p['stricter_bar'])}" if p.get("stricter_bar") else ""
            lines.append(f"- {p['action_type']}: {p['description']} Autonomy: {p['who_acts']}{extra}; {p['cap']}{stricter}.")
    else:
        lines += ["", "WHAT YOU MAY DO: no action is granted; findings are the whole output."]

    if brief["never"]:
        lines += ["", "NEVER IN A RULE: " + _listed([x["column"] for x in brief["never"]])]
    for metric, cols in brief["not_a_driver_of"].items():
        lines.append(f"NOT A DRIVER OF {metric} (they define it): {_listed(cols)}")

    lines += ["", f"PROOF THE GATE REQUIRES: {_bar_text(brief['evidence_bar'])}."]
    for term, col in brief["glossary_in_data"].items():
        lines.append(f'IN THIS DATA, "{term}" is `{col}`.')
    for note in brief["notes"]:
        lines.append(f"NOTE: {note}")
    return "\n".join(lines)


def coverage(charter: RoleCharter, ledger, evidence: list | None = None) -> list[dict[str, Any]]:
    """Per accountability: what the run did about it. Read from the ledger, so a question
    the agent never touched is reported as such rather than silently absent. Pass the
    run's `evidence` after the multiple-testing correction so `supported` counts what held."""
    questions = ledger.all("question")
    hypotheses = ledger.all("hypothesis")
    evidence = {e.hypothesis_id: e for e in (evidence if evidence is not None else ledger.all("evidence"))}
    findings = ledger.all("finding")
    decisions = {d.id: d for d in ledger.all("decision")}
    actions = ledger.all("action")
    q_of_finding = {f.id: f.question_id for f in findings}

    out = []
    for acc in sorted(charter.accountabilities, key=lambda a: a.priority):
        qids = {q.id for q in questions if q.accountability_id == acc.id}
        status_q = next((q.status for q in questions if q.accountability_id == acc.id), "open")
        tested = [h for h in hypotheses if h.question_id in qids and h.id in evidence]
        supported = sum(1 for h in tested if evidence[h.id].verdict.value == "SUPPORTED")
        found = [f for f in findings if f.question_id in qids]
        acted = [a for a in actions
                 if (d := decisions.get(a.decision_id)) and any(q_of_finding.get(i) in qids for i in d.finding_ids)]
        if status_q == "withheld":
            outcome = "withheld — not covered this run"
        elif found:
            outcome = f"{len(found)} finding(s)" + (f", {len(acted)} action(s)" if acted else "")
        elif tested:
            outcome = "tested; nothing cleared the evidence bar"
        else:
            outcome = "not reached"
        out.append({
            "id": acc.id, "metric": acc.metric, "goal": f"{_VERB[acc.direction]} {acc.metric}",
            "tested": len(tested), "supported": supported, "findings": len(found),
            "actions": len(acted), "outcome": outcome,
        })
    return out
