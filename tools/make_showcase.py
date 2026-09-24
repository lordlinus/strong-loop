"""make_showcase.py — build docs/index.html ("How it works"), the loop on one ring, from a recorded run.

    python tools/make_showcase.py docs/runs/<run_dir>

The page lives in tools/index.template.html and draws with docs/wheel.js — the same wheel
docs/live.html drives from the live stream. This script only supplies the numbers: the
run's trace.log, ledger.jsonl, iteration headers and report.json, the charter as loaded, and
the engine's own source. Nothing on the screen is typed in by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICE = ROOT / "src" / "strong-loop"
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(ROOT / "tools"))

import pandas as pd  # noqa: E402

from loop import charter_md, gates, runner, tools  # noqa: E402
from loop.charter import authorise_action, load_charter  # noqa: E402
from make_loop_doc import by_id, load_run, source_of  # noqa: E402

TEMPLATE = ROOT / "tools" / "index.template.html"
PLACEHOLDER = "/*RUN_JSON*/"


def payload(run_dir: pathlib.Path, charter_path: pathlib.Path, data_path: pathlib.Path) -> dict:
    charter = load_charter(charter_path)
    data = pd.read_csv(data_path)
    run = load_run(run_dir)
    ledger = run["ledger"]
    objs = by_id(ledger)
    std = charter.evidence_standards

    evidence = {e.id: e for e in ledger.all("evidence")}
    actions = []
    for a in ledger.all("action"):
        d = objs.get(a.decision_id)
        findings = [objs[i] for i in (d.finding_ids if d else []) if i in objs]
        auth = authorise_action(charter=charter, action_type=a.action_type, findings=findings,
                                evidence_by_id=evidence, requested_blast_radius=a.blast_radius,
                                observation_plan=a.observation_plan)
        actions.append({
            "id": a.id, "action_type": a.action_type, "status": a.status,
            "autonomy_level": a.autonomy_level.value, "blast_radius": a.blast_radius,
            "observation_plan": a.observation_plan,
            "recommendation": d.recommendation if d else "", "finding_ids": d.finding_ids if d else [],
            "reasons": auth.reasons,
        })

    return {
        "role": charter.role,
        "description": charter.description,
        "dataset": {"name": data_path.name, "rows": len(data), "columns": len(data.columns)},
        "run_dir": str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "standard": {
            "min_sample_size": std.min_sample_size, "max_p_value": std.max_p_value,
            "min_effect_size": std.min_effect_size, "correction": std.multiple_testing_correction,
            "requires_confound_control": std.requires_confound_control,
        },
        "accountabilities": [
            {"id": a.id, "statement": a.statement, "metric": a.metric, "direction": a.direction,
             "horizon_days": a.horizon_days, "priority": a.priority}
            for a in charter.accountabilities],
        "rights": [
            {"action_type": r.action_type, "description": r.description, "autonomy_level": r.autonomy_level.value,
             "reversible": r.reversible, "max_per_run": r.blast_radius.max_per_run, "unit": r.blast_radius.unit,
             "observation_metric": r.observation_metric, "requires_authority": r.requires_authority}
            for r in charter.decision_rights],
        "constraints": {
            "forbidden_features": charter.constraints.forbidden_features,
            "leakage_features": charter.constraints.leakage_features,
            "block_sensitive_columns": charter.constraints.block_sensitive_columns,
        },
        "constants": {"patience": runner.PATIENCE, "idle_patience": runner.IDLE_PATIENCE,
                      "min_iterations": runner.MIN_ITERATIONS},
        "charter_md": charter_md.render(charter),
        "charter_file": charter_path.name,
        "questions": [{"id": q.id, "text": q.text, "why": q.why_it_matters, "priority": q.priority}
                      for q in sorted(ledger.all("question"), key=lambda q: q.priority)],
        "headers": {str(k): v for k, v in run["headers"].items()},
        "gates": [{"name": g["name"], "question_shape": g["question_shape"]} for g in gates.available()],
        "source": {"decide": source_of(gates.decide), "unchallenged": source_of(tools.Toolbelt._unchallenged)},
        "trace": run["trace"],
        "ledger": {
            "hypotheses": {h.id: {"statement": h.statement, "kind": h.kind, "spec": h.spec}
                           for h in ledger.all("hypothesis")},
            "evidence": {e.id: {"hypothesis_id": e.hypothesis_id, "gate": e.gate, "verdict": e.verdict.value,
                                "effect": e.effect_size, "p": e.p_value, "n": e.sample_size,
                                "adjusted_p": e.adjusted_p_value, "statistics": e.statistics,
                                "refusal_reason": e.refusal_reason, "warnings": e.warnings}
                         for e in ledger.all("evidence")},
            "findings": {f.id: {"headline": f.headline, "confidence": f.confidence, "evidence_id": f.evidence_id,
                                "interpretation": f.interpretation}
                         for f in ledger.all("finding")},
            "actions": actions,
        },
        "report": run["report"],
    }


def build(run_dir: pathlib.Path, charter_path: pathlib.Path, data_path: pathlib.Path, out: pathlib.Path) -> None:
    template = TEMPLATE.read_text()
    if PLACEHOLDER not in template:
        raise SystemExit(f"{TEMPLATE} has no {PLACEHOLDER} placeholder")
    data = json.dumps(payload(run_dir, charter_path, data_path), ensure_ascii=False)
    # Paths recorded on the machine that ran it (report.json, the trace) become repo-relative:
    # the page is public and must not carry someone's home directory.
    data = data.replace(json.dumps(str(ROOT) + "/")[1:-1], "")
    # A closing script tag inside a JSON string would end the data block early.
    data = data.replace("</", "<\\/")
    page = template.replace(PLACEHOLDER, data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({len(page)//1024} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=pathlib.Path)
    ap.add_argument("--charter", type=pathlib.Path, default=SERVICE / "charters" / "claims_analyst.yaml")
    ap.add_argument("--data", type=pathlib.Path, default=SERVICE / "data" / "claims.csv")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "docs" / "index.html")
    a = ap.parse_args()
    build(a.run_dir.resolve(), a.charter, a.data, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
