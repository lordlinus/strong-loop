"""cli.py — `python -m loop <command>`. Everything the hosted agent can do, from a shell.

    models     which client LOOP_MODEL resolves to, and a one-token probe
    check      charter + data are a valid pairing; the gate and the screens work; no model
    questions  the question set a run would inherit, numbered
    sign       ratify a charter's current content
    presets    the shipped roles and datasets, as the JSON the customer page reads
    render     a charter in the other format (YAML <-> standard Markdown, `loop/charter_md.py`)
    run        the loop. Needs a model.

`check` stays API-free by design — it is the free CI/dev-loop verifier. The hosted
path's own pairing step (`loop.inputs.settle`) is not: it automatically asks TypeSafe to
review any accountability metric `check` would report as an unresolved mismatch (no
column shares vocabulary with it), before a human ever sees the intake report. See
`loop/suggest.py`.
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
from .intake import NOTHING_MEASURES_IT, normalize_columns, pair, resolve
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
    data, _labels = normalize_columns(pd.read_csv(path))
    return data


def cmd_check(args: argparse.Namespace) -> int:
    """A printer over `intake.pair` — the same pairing the hosted agent shows before a run."""
    charter = load_charter(args.charter)
    data, labels = normalize_columns(pd.read_csv(args.data))
    r = pair(charter, data, labels)
    if args.json:
        print(r.model_dump_json(indent=2))
        return 0 if r.ok else 1

    print(f"charter : {r.role} v{r.version} [{r.status}]")
    if r.content_hash:
        print(f"authority hash: {r.content_hash[:12]}")
    if charter.glossary:
        print(f"glossary: {', '.join(sorted(charter.glossary))}")
    print(f"data    : {args.data} ({r.rows} rows, {len(data.columns)} cols, "
          f"{len(r.analysable_columns)} analysable)")
    if r.column_labels:
        print("renamed to safe identifiers:")
        for safe, original in r.column_labels.items():
            print(f"  - {original!r} -> {safe}")
    if r.recoded:
        print("values read as numbers:")
        for col, how in r.recoded.items():
            print(f"  - {col}: {how}")
    if r.screened:
        print("screened out:")
        for s in r.screened:
            print(f"  - {s.column}: {s.reason}")
    if r.derived:
        print("derived screens (this data, this charter — no list was consulted):")
        for s in r.derived:
            print(f"  - {s.column}: {s.reason}")
    if r.suspected:
        print("suspected (allowed; evidence using them carries a warning):")
        for s in r.suspected:
            print(f"  - {s.column}: {s.reason}")
    for w in r.warnings:
        print(f"  ! {w}")

    print("\naccountabilities:")
    for a in r.accountabilities:
        mark = "ok " if a.status == "ok" else a.status.upper()
        why = f" — {a.measure['reason']}" if a.status == "unmeasurable" and a.measure.get("reason") else ""
        print(f"  [{mark}] {a.id}: {a.metric}{why}")
    for c in r.clarifications:
        print(f"  ? {c.accountability_id}: {c.question}")
        for cand in c.candidates:
            print(f"      - {cand}")

    print(f"\n{len(r.questions)} questions:")
    for q in r.questions:
        print(f"  - {q}")
    for q in r.withheld:
        print(f"  - (withheld) {q}")

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
    """The same intake the hosted agent performs, then the loop.

    A metric the data does not carry under that name is resolved the one way the platform
    allows: `--map <accountability_id>=<column>` picks from the closed list `pair()` (and,
    when configured, TypeSafe) offered, and `--ratified-by` puts a name to the remapped
    charter. No free text, no derivation step, no domain script.
    """
    from .inputs import derive_all
    from .runner import run_once
    from .suggest import review

    charter = load_charter(args.charter)
    data, labels = normalize_columns(pd.read_csv(args.data))
    derived = asyncio.run(derive_all(charter, data, labels))
    report = pair(charter, data, labels, derived)
    answers = dict(item.split("=", 1) for item in (args.map or []) if "=" in item)
    if report.clarifications or answers:
        report = asyncio.run(review(charter, data, report, labels))
    if answers:
        res = resolve(charter, report, answers)
        if res.refused:
            raise SystemExit("\n".join(["mapping refused:", *(f"  {r}" for r in res.refused),
                                       *_menu(report)]))
        charter = res.charter
        if res.remapped or res.dropped:
            if not args.ratified_by:
                raise SystemExit("--ratified-by <name> is required: remapping changes what "
                                 "the role is answerable for, and someone signs that.")
            charter = sign(charter, args.ratified_by)
            derived = asyncio.run(derive_all(charter, data, labels))
            report = pair(charter, data, labels, derived)
    if report.problems:
        raise SystemExit("\n".join(["intake refused the pairing:",
                                   *(f"  !! {p}" for p in report.problems), *_menu(report)]))
    result = asyncio.run(
        run_once(
            charter, data, max_iterations=args.iterations,
            runs_dir=pathlib.Path(args.run_dir), model=args.model, steer=args.steer,
            derived=derived,
        )
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def _menu(report) -> list[str]:
    lines = []
    for c in report.clarifications:
        lines.append(f"  ? {c.accountability_id}: {c.question}")
        lines.extend(f"      --map {c.accountability_id}={cand!r}" for cand in c.candidates
                     if cand != NOTHING_MEASURES_IT)
        lines.append(f"      --map {c.accountability_id}={NOTHING_MEASURES_IT!r}   (leave it out)")
    return lines


def cmd_questions(args: argparse.Namespace) -> int:
    charter = load_charter(args.charter)
    for i, q in enumerate(questions_from(charter), start=1):
        print(f"{i:>2}. [{q.accountability_id}] {q.text}")
    return 0


def presets_manifest() -> dict:
    """What the customer page offers without a round-trip: every shipped charter and dataset,
    described from its own content, plus which pairings the same check passes."""
    from . import charter_md
    from .charter import RoleCharter
    from .inputs import SERVICE, presets

    names = presets()
    charters = []
    for stem in names["charters"]:
        path = SERVICE / "charters" / f"{stem}.yaml"
        c = load_charter(path)
        charters.append({
            "name": stem, "description": " ".join(c.description.split()),
            "metrics": [a.metric for a in c.accountabilities],
            "accountabilities": [a.statement for a in c.accountabilities],
            "yaml": path.read_text(),
            "markdown": charter_md.render(c),   # the charter page's worked examples
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
    # A preset is a charter that runs against a shipped dataset as-is. A charter or CSV
    # sitting in the folder with no clean pairing (someone's local, gitignored work) is
    # not offered, and does not make the committed manifest stale.
    paired_charters = {p["charter"] for p in pairings}
    paired_data = {p["data"] for p in pairings}
    charters = [c for c in charters if c["name"] in paired_charters]
    datasets = [d for d in datasets if d["name"] in paired_data]
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
    target = pathlib.Path(args.out or args.charter)
    if target.suffix.lower() == ".md":
        from . import charter_md

        charter = charter_md.through_markdown(charter)   # sign what the file will say
    signed = sign(charter, args.by)
    written = write_charter(signed, target)
    print(f"SIGNED by {signed.ratified_by} at {signed.ratified_at}")
    print(f"hash {signed.content_hash}")
    print(f"wrote {written}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Same charter, other format. Reads with `verify=False` so a signed charter can be
    shown; writing it back out does not re-sign it."""
    import yaml

    from . import charter_md

    charter = load_charter(args.charter, verify=False)
    to = args.to or ("yaml" if pathlib.Path(args.charter).suffix.lower() == ".md" else "md")
    if to == "md" and charter.content_hash and \
            charter_md.through_markdown(charter).fingerprint() != charter.content_hash:
        # A hash that no longer matches is worse than none: it reads as tampering.
        charter = charter.model_copy(update={"status": "DRAFT", "ratified_by": None,
                                             "ratified_at": None, "content_hash": None})
        print("note: the signature does not survive conversion; re-sign the Markdown", file=sys.stderr)
    if args.out:
        print(f"wrote {write_charter(charter, pathlib.Path(args.out).with_suffix('.' + to))}")
        return 0
    if to == "md":
        sys.stdout.write(charter_md.render(charter))
    else:
        sys.stdout.write(yaml.safe_dump(charter.model_dump(mode="json", exclude_none=True),
                                        sort_keys=False, allow_unicode=True))
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
    p.add_argument("--map", action="append", metavar="ACC_ID=COLUMN",
                   help="resolve a metric the data does not carry under that name, from the "
                        "closed list `check` offers; repeatable")
    p.add_argument("--ratified-by", default=None,
                   help="who signs the remapped charter; required with --map")
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

    p = sub.add_parser("render", help="a charter in the other format: YAML -> standard Markdown, or back")
    p.add_argument("charter")
    p.add_argument("--to", choices=["md", "yaml"], default=None,
                   help="default: the format the charter is not already in")
    p.add_argument("--out", default=None, help="write here instead of stdout")
    p.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
