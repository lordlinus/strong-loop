"""cli.py — `python -m loop <command>`. Everything the hosted agent can do, from a shell."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import models


def cmd_models(args: argparse.Namespace) -> int:
    info = models.describe(args.model)
    print(json.dumps(info, indent=2))
    if args.no_probe:
        return 0
    try:
        reply = asyncio.run(models.probe(args.model))
    except Exception as exc:  # the probe exists to surface exactly these
        print(f"probe FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"probe ok: {reply[:80]!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loop")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("models", help="show which client LOOP_MODEL resolves to, and probe it")
    p.add_argument("--model", default=None, help="override LOOP_MODEL for this call")
    p.add_argument("--no-probe", action="store_true", help="describe only; no model call")
    p.set_defaults(func=cmd_models)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
