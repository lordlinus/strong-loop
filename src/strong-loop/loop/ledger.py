"""
ledger.py — append-only run state, and the thing that makes iterations coherent.

Two jobs:

  1. WITHIN a run: the ledger is the shared state between otherwise isolated iterations.
     Each Ralph iteration starts with a fresh context and reads the ledger to know what
     has already been tried. Without it, a fresh-context loop re-proposes the same three
     hypotheses forever.

  2. ACROSS runs: the outcome ledger is where the platform's actual learning lives. Prior
     beliefs get scored against reality; hypothesis classes that never pay off get
     down-weighted. The LLM is rented and swappable — this file is the asset.

Storage is JSONL on disk: trivially auditable, append-only, diffable, and survivable
across process restarts. Swap for durable storage in production; keep the semantics.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterator

from pydantic import BaseModel

from .types import (
    Action,
    Decision,
    Evidence,
    Finding,
    Hypothesis,
    Outcome,
    Question,
    Verdict,
)

_KINDS = {
    "question": Question,
    "hypothesis": Hypothesis,
    "evidence": Evidence,
    "finding": Finding,
    "decision": Decision,
    "action": Action,
    "outcome": Outcome,
}


class Ledger:
    def __init__(self, run_dir: str | pathlib.Path):
        self.run_dir = pathlib.Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "ledger.jsonl"
        self.path.touch(exist_ok=True)
        self._cache: list[tuple[str, BaseModel]] | None = None

    # ---- write ---------------------------------------------------------------------
    def append(self, obj: BaseModel) -> BaseModel:
        kind = next((k for k, cls in _KINDS.items() if isinstance(obj, cls)), None)
        if kind is None:
            raise TypeError(f"{type(obj).__name__} is not a ledger record type")
        line = json.dumps({"kind": kind, "data": obj.model_dump(mode="json")}, default=str)
        with self.path.open("a") as fh:
            fh.write(line + "\n")
        if self._cache is not None:
            self._cache.append((kind, obj))
        return obj

    def extend(self, objs: list[BaseModel]) -> None:
        for obj in objs:
            self.append(obj)

    # ---- read ----------------------------------------------------------------------
    def _load(self) -> list[tuple[str, BaseModel]]:
        if self._cache is None:
            records: list[tuple[str, BaseModel]] = []
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                cls = _KINDS[payload["kind"]]
                records.append((payload["kind"], cls.model_validate(payload["data"])))
            self._cache = records
        return self._cache

    def all(self, kind: str) -> list[Any]:
        return [obj for k, obj in self._load() if k == kind]

    def __iter__(self) -> Iterator[tuple[str, BaseModel]]:
        return iter(self._load())

    def by_id(self, record_id: str) -> BaseModel | None:
        return next((obj for _, obj in self._load() if getattr(obj, "id", None) == record_id), None)

    # ---- the views the loop actually needs -----------------------------------------
    def same_rows(self, hypothesis: Hypothesis) -> Hypothesis | None:
        """An earlier hypothesis of the same kind and parameters whose rule selected
        exactly the same rows — `Acquired == 1` and `pillars_met >= 2` were one test."""
        if not hypothesis.rows_hash:
            return None
        key = rows_fingerprint(hypothesis)
        return next((h for h in self.all("hypothesis")
                     if h.rows_hash and rows_fingerprint(h) == key), None)

    def tried_specs(self) -> set[str]:
        """Canonical fingerprints of everything already tested.

        Cheap de-duplication. Prevents a fresh-context agent from burning an entire
        iteration re-testing a rule that was refused three iterations ago.
        """
        out = set()
        for h in self.all("hypothesis"):
            out.add(fingerprint(h))
        return out

    def summary(self, max_items: int = 8) -> dict[str, Any]:
        """The compact state handed to the next iteration.

        Kept small on purpose — this is loaded into every fresh context, so its size is a
        direct tax on the agent's remaining room to think. Eight recent tests, not
        twenty-five: at sixty hypotheses the larger figure cost ~4k tokens per iteration.
        """
        hypotheses = {h.id: h for h in self.all("hypothesis")}
        evidences = self.all("evidence")
        findings = self.all("finding")

        recent = []
        for e in evidences[-max_items:]:
            h = hypotheses.get(e.hypothesis_id)
            recent.append(
                {
                    "statement": h.statement if h else "?",
                    "gate": e.gate,
                    "spec": h.spec if h else {},
                    "verdict": e.verdict.value,
                    "effect": e.effect_size,
                    "n": e.sample_size,
                    "p": round(e.p_value, 5) if e.p_value is not None else None,
                    "why_refused": e.refusal_reason,
                }
            )

        counts: dict[str, int] = {}
        for e in evidences:
            counts[e.verdict.value] = counts.get(e.verdict.value, 0) + 1

        return {
            "questions_open": len([q for q in self.all("question") if q.status == "open"]),
            "hypotheses_tested": len(evidences),
            "verdict_counts": counts,
            "findings": [
                {"headline": f.headline, "confidence": f.confidence} for f in findings
            ],
            "actions_proposed": len(self.all("action")),
            # type + target rule only (~25 tokens each): enough to stop the next iteration
            # proposing the same action for the same group.
            "actions": [{"type": a.action_type, "where": a.params.get("where"), "status": a.status}
                        for a in self.all("action")],
            "recent_tests": recent,
        }

    def yield_rate(self, last_n: int) -> float:
        """Share of recent tests that produced a supported finding.

        The loop's convergence signal: when yield collapses, the agent has exhausted the
        productive part of this question space and should stop rather than grind.
        """
        evidences = self.all("evidence")[-last_n:]
        if not evidences:
            return 0.0
        supported = sum(1 for e in evidences if e.verdict == Verdict.SUPPORTED)
        return supported / len(evidences)


def fingerprint(hypothesis: Hypothesis) -> str:
    """Order-insensitive fingerprint of a hypothesis's testable content.

    Only `kind` + `spec` matter. Two hypotheses with different prose but identical specs
    are the same experiment, and rewording is exactly how a language model accidentally
    repeats itself.
    """
    spec = json.dumps(hypothesis.spec, sort_keys=True, default=str)
    return f"{hypothesis.kind}::{spec}"


def rows_fingerprint(hypothesis: Hypothesis) -> str:
    """Like `fingerprint`, with the rule replaced by the rows it selected."""
    spec = {k: v for k, v in hypothesis.spec.items() if k != "where"}
    spec["rows"] = hypothesis.rows_hash
    return f"{hypothesis.kind}::{json.dumps(spec, sort_keys=True, default=str)}"
