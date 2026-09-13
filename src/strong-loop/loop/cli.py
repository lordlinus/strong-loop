"""cli.py — `python -m loop <command>`. Everything the hosted agent can do, from a shell.

    models     which client LOOP_MODEL resolves to, and a one-token probe
    check      charter + data are a valid pairing; the gate and the screens work; no model
    questions  the question set a run would inherit, numbered
    sign       ratify a charter's current content
    presets    the shipped roles and datasets, as the JSON the customer page reads
    run        the loop. Needs a model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import pandas as pd

from . import models
from .charter import load_charter, sign, write_charter
from .intake import pair
from .questions import questions_from


def cmd_models(args: argparse.Namespace) -> int:
    print(json.dumps(models.describe(args.model), indent=2))
    if args.no_probe:
        return 0
    try:
        reply = asyncio.run(models.probe(args.model))
    except Exception as exc:  # the probe exists to surface exactly these
        print(f"probe FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"probe ok: {reply[:80]!r}")
    return 0


def load_data(path: str | pathlib.Path) -> pd.DataFrame:
    return pd.read_csv(path)


def missing_metrics(charter, data: pd.DataFrame) -> list[str]:
    """Accountability metrics that are not a column. Exact match, deliberately."""
    return [a.metric for a in charter.accountabilities if a.metric not in data.columns]


def cmd_check(args: argparse.Namespace) -> int:
    """A printer over `intake.pair` — the same pairing the hosted agent shows before a run."""
    charter = load_charter(args.charter)
    data = load_data(args.data)
    r = pair(charter, data)
    if args.json:
        print(r.model_dump_json(indent=2))
        return 0 if r.ok else 1

    print(f"charter : {r.role} v{r.version} [{r.status}]")
    if r.content_hash:
        print(f"authority hash: {r.content_hash[:12]}")
    print(f"data    : {args.data} ({r.rows} rows, {len(data.columns)} cols, "
          f"{len(r.analysable_columns)} analysable)")
    if r.screened:
        print("screened out:")
        for s in r.screened:
            print(f"  - {s.column}: {s.reason}")

    print("\naccountabilities:")
    for a in r.accountabilities:
        mark = "ok " if a.status == "ok" else a.status.upper()
        print(f"  [{mark}] {a.id}: {a.metric}")
    for c in r.clarifications:
        print(f"  ? {c.accountability_id}: {c.question}")
        for cand in c.candidates:
            print(f"      - {cand}")

    print(f"\n{len(r.questions)} questions:")
    for q in r.questions:
        print(f"  - {q}")

    print("\ngate smoke test:")
    if r.gate_probe:
        p = r.gate_probe
        print(f"  {p.gate} where={p.where!r} -> {p.verdict}" + (f" ({p.reason})" if p.reason else ""))
    else:
        print("  (no probe available)")
    if r.screen_probe:
        p = r.screen_probe
        print(f"  sensitive-column probe {p.where!r} -> {p.verdict} ({p.reason})")

    if r.problems:
        print("\n" + "\n".join(f"!! {p}" for p in r.problems), file=sys.stderr)
        return 1
    print("\nOK")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .runner import run_once

    charter = load_charter(args.charter)
    data = load_data(args.data)
    if missing := missing_metrics(charter, data):
        raise SystemExit(f"accountability metric(s) not in this data: {missing}")
    report = asyncio.run(
        run_once(
            charter, data, max_iterations=args.iterations,
            runs_dir=pathlib.Path(args.run_dir), model=args.model, steer=args.steer,
        )
    )
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_questions(args: argparse.Namespace) -> int:
    charter = load_charter(args.charter)
    for i, q in enumerate(questions_from(charter), start=1):
        print(f"{i:>2}. [{q.accountability_id}] {q.text}")
    return 0


def presets_manifest() -> dict:
    """What the customer page offers without a round-trip: every shipped charter and dataset,
    described from its own content, plus which pairings the same check passes."""
    from .inputs import SERVICE, presets
    from .charter import RoleCharter

    names = presets()
    charters = []
    for stem in names["charters"]:
        path = SERVICE / "charters" / f"{stem}.yaml"
        c = load_charter(path)
        charters.append({
            "name": stem, "description": " ".join(c.description.split()),
            "metrics": [a.metric for a in c.accountabilities],
            "accountabilities": [a.statement for a in c.accountabilities],
            "yaml": path.read_text(),   # the editor's starting point
        })
    datasets = []
    frames = {}
    for stem in names["data"]:
        frames[stem] = df = load_data(SERVICE / "data" / f"{stem}.csv")
        datasets.append({"name": stem, "rows": int(len(df)), "columns": list(map(str, df.columns))})
    pairings = [
        {"charter": c["name"], "data": d, "metrics": c["metrics"]}
        for c in charters for d in frames
        if all(m in frames[d].columns for m in c["metrics"]) and pair(load_charter(SERVICE / "charters" / f"{c['name']}.yaml"), frames[d]).ok
    ]
    schema = RoleCharter.model_json_schema()
    return {"charters": charters, "data": datasets, "pairings": pairings, "charter_schema": schema}


def cmd_presets(args: argparse.Namespace) -> int:
    text = json.dumps(presets_manifest(), indent=1)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    # The one sanctioned reader of an out-of-date signature: this command's job is to
    # produce a new one.
    charter = load_charter(args.charter, verify=False)
    signed = sign(charter, args.by)
    written = write_charter(signed, args.out or args.charter)
    print(f"SIGNED by {signed.ratified_by} at {signed.ratified_at}")
    print(f"hash {signed.content_hash}")
    print(f"wrote {written}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loop")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("models", help="show which client LOOP_MODEL resolves to, and probe it")
    p.add_argument("--model", default=None, help="override LOOP_MODEL for this call")
    p.add_argument("--no-probe", action="store_true", help="describe only; no model call")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("check", help="validate charter + data, exercise the gate, no model")
    p.add_argument("--charter", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--json", action="store_true", help="print the IntakeReport instead of prose")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="run the loop against a charter and a dataset")
    p.add_argument("--charter", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--iterations", type=int, default=6)
    p.add_argument("--model", default=None, help="override LOOP_MODEL")
    p.add_argument("--run-dir", default="runs")
    p.add_argument("--steer", default="Begin. Work the open questions.",
                   help="the human's one message; every iteration starts from it")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("questions", help="the question set a run would inherit")
    p.add_argument("--charter", required=True)
    p.set_defaults(func=cmd_questions)

    p = sub.add_parser("sign", help="ratify a charter's current content")
    p.add_argument("charter")
    p.add_argument("--by", required=True, help="who is ratifying")
    p.add_argument("--out", default=None, help="write here instead of in place")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("presets", help="shipped roles, datasets and valid pairings, as JSON")
    p.add_argument("--out", default=None, help="write here (e.g. docs/presets.json) instead of stdout")
    p.set_defaults(func=cmd_presets)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
