"""cli.py — `python -m loop <command>`. Everything the hosted agent can do, from a shell.

    models     which client LOOP_MODEL resolves to, and a one-token probe
    check      charter + data are a valid pairing; the gate and the screens work; no model
    questions  the question set a run would inherit, numbered
    sign       ratify a charter's current content
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import shutil
import sys

import pandas as pd

from . import gates, models
from .charter import load_charter, sign, write_charter
from .ledger import Ledger
from .questions import questions_from
from .types import Hypothesis


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
    charter = load_charter(args.charter)
    data = load_data(args.data)
    print(f"charter : {charter.role} v{charter.version} [{charter.status}]")
    if charter.content_hash:
        print(f"authority hash: {charter.content_hash[:12]}")
    print(f"data    : {args.data} ({len(data)} rows, {len(data.columns)} cols)")

    sensitive = [c for c in data.columns if gates.is_sensitive_column(c)]
    if sensitive:
        print(f"excluded as sensitive: {sensitive}")

    print("\naccountabilities:")
    missing = missing_metrics(charter, data)
    for acc in charter.accountabilities:
        mark = "MISSING" if acc.metric in missing else "ok "
        print(f"  [{mark}] {acc.id}: {acc.metric}")

    questions = questions_from(charter)
    print(f"\n{len(questions)} questions:")
    for q in questions:
        print(f"  - {q.text}")

    # Exercise the gate end to end without a model, so a broken engine fails here rather
    # than in iteration 3. Start from an empty ledger every time: a reused one answers
    # "duplicate" instead of a verdict, and a pre-commit check must not fail on its
    # second run.
    check_dir = pathlib.Path(args.run_dir) / "check"
    shutil.rmtree(check_dir, ignore_errors=True)
    ledger = Ledger(check_dir)
    for q in questions:
        ledger.append(q)
    target_col = next((a.metric for a in charter.accountabilities if a.metric not in missing), None)
    ctx = gates.GateContext(
        data=data,
        charter=charter,
        standard=charter.evidence_standards,
        target=data[target_col] if target_col else None,
    )

    print("\ngate smoke test:")
    probe = args.probe or _default_probe(data, charter)
    if probe and target_col:
        h = Hypothesis(question_id=questions[0].id, statement=f"probe: {probe}",
                       kind="proportion_lift", spec={"where": probe}, rationale="CLI smoke test")
        ledger.append(h)
        e = ledger.append(gates.evaluate(h, ctx))
        print(f"  where={probe!r} -> {e.verdict.value}" + (f" ({e.refusal_reason})" if e.refusal_reason else ""))
    else:
        print("  (no probe available; pass --probe)")

    # Prove the screens bite, on a real identifier from THIS dataset. Assert on the
    # reason, not just the verdict: an internal error is also REFUSED.
    if sensitive and target_col:
        col = sensitive[0]
        h = Hypothesis(question_id=questions[0].id, statement="probe: sensitive column",
                       kind="proportion_lift", spec={"where": f"{col} == {col}"})
        e = gates.evaluate(h, ctx)
        print(f"  sensitive-column probe ({col}) -> {e.verdict.value} ({e.refusal_reason})")
        if e.verdict.value != "REFUSED" or "sensitive" not in (e.refusal_reason or ""):
            print(f"  !! screens did not block identifier column {col}", file=sys.stderr)
            return 1

    if missing:
        print(f"\n{len(missing)} accountability metric(s) not in this data: {missing}", file=sys.stderr)
        return 1
    print("\nOK")
    return 0


def _default_probe(data: pd.DataFrame, charter) -> str | None:
    blocked = set(charter.constraints.forbidden_features) | set(charter.constraints.leakage_features)
    for col in data.columns:
        if col in blocked or gates.is_sensitive_column(col):
            continue
        if pd.api.types.is_numeric_dtype(data[col]) and data[col].nunique() > 2:
            return f"{col} > {data[col].median()}"
    return None


def cmd_questions(args: argparse.Namespace) -> int:
    charter = load_charter(args.charter)
    for i, q in enumerate(questions_from(charter), start=1):
        print(f"{i:>2}. [{q.accountability_id}] {q.text}")
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
    p.add_argument("--probe", default=None, help="a where-expression for the smoke test")
    p.add_argument("--run-dir", default="runs")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("questions", help="the question set a run would inherit")
    p.add_argument("--charter", required=True)
    p.set_defaults(func=cmd_questions)

    p = sub.add_parser("sign", help="ratify a charter's current content")
    p.add_argument("charter")
    p.add_argument("--by", required=True, help="who is ratifying")
    p.add_argument("--out", default=None, help="write here instead of in place")
    p.set_defaults(func=cmd_sign)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
