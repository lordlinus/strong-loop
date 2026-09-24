"""derived.py — what THIS data says may not be tested, worked out per run.

A charter's `leakage_features` is a list someone wrote before they saw the dataset. The
first real export proved the list cannot be the mechanism: the metric
`capability_coverage_pct` arrived beside `pillars_met`, the column it is computed from,
and the loop "discovered" that customers with a pillar met have higher coverage. Nobody
had listed `pillars_met`, and nobody will list the equivalent column in the next export
either. So the screens are derived from the data at run time, with no vocabulary.

Everything here is code over the data; nothing is asked of a model. The object it returns
sits BESIDE the charter rather than inside it — the charter's signature covers its
constraints, and code must not sign. `gates.screen` consults it with the hypothesis's
outcome in hand, because a leak is a (column, metric) pair: `pillars_met` is the
definition of `capability_coverage_pct` and a legitimate driver of `agent_token_pct`.

Rules (guards tuned on every shipped pairing, 2026-09-22 — see PLAN.md §6.6):

    R0  a metric with one value is unmeasurable; a binary metric whose minority class is
        below the charter's minimum sample is warned about
    R1  a constant column is excluded
    R2  |Spearman| >= COPY_RHO with a metric is a copy or rescaling of it
    R3  every value of the column maps to ONE value of the metric (functional dependency),
        unless the dependency is made of one-row groups — then it is vacuous
    R4  every value of the metric maps to ONE value of the column: a threshold or relabelling
    R5  one value of a low-cardinality column holds every positive (or every negative) of a
        binary metric: perfect separation
    R6  two columns that partition the rows identically are aliases; only one is offered
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import gates
from .charter import RoleCharter

COPY_RHO = 0.99          # highest legitimate |rho| seen on shipped data was 0.695
SUSPECT_RHO = 0.95
SINGLETON_SHARE = 0.05   # a numerator with unique non-zero values is not a copy
SEPARATION_CARDINALITY = 20


@dataclass
class Derived:
    """Run-time exclusions. `leaks` and `suspected` are keyed column -> {metric: reason}."""

    leaks: dict[str, dict[str, str]] = field(default_factory=dict)
    suspected: dict[str, dict[str, str]] = field(default_factory=dict)
    constants: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)        # column -> the one kept
    identifiers: dict[str, str] = field(default_factory=dict)    # column -> reason (semantic tier)
    protected: dict[str, str] = field(default_factory=dict)      # column -> reason (semantic tier)
    unmeasurable: dict[str, str] = field(default_factory=dict)   # metric -> reason
    warnings: list[str] = field(default_factory=list)

    # ---- what the screens ask ------------------------------------------------------
    def excluded(self) -> dict[str, str]:
        """Columns that leave the profile entirely, with reasons."""
        out = dict(self.constants)
        out.update(self.identifiers)
        out.update(self.protected)
        out.update({c: f"same partition as {kept!r}; use that column" for c, kept in self.aliases.items()})
        return out

    def leak_reason(self, column: str, outcome: str | None) -> str | None:
        return (self.leaks.get(column) or {}).get(outcome) if outcome else None

    def suspect_reason(self, column: str, outcome: str | None) -> str | None:
        return (self.suspected.get(column) or {}).get(outcome) if outcome else None

    # ---- composition ---------------------------------------------------------------
    def merge(self, other: "Derived") -> "Derived":
        """`self` (the code tier) wins wherever both have an opinion."""
        out = Derived(
            leaks={c: dict(m) for c, m in self.leaks.items()},
            suspected={c: dict(m) for c, m in self.suspected.items()},
            constants=dict(self.constants), aliases=dict(self.aliases),
            identifiers=dict(self.identifiers), protected=dict(self.protected),
            unmeasurable=dict(self.unmeasurable), warnings=list(self.warnings),
        )
        for c, metrics in other.leaks.items():
            for m, reason in metrics.items():
                out.leaks.setdefault(c, {}).setdefault(m, reason)
        for c, metrics in other.suspected.items():
            for m, reason in metrics.items():
                if m not in out.leaks.get(c, {}):
                    out.suspected.setdefault(c, {}).setdefault(m, reason)
        for c, reason in other.constants.items():
            out.constants.setdefault(c, reason)
        for c, reason in other.identifiers.items():
            if c not in out.constants and c not in out.aliases:
                out.identifiers.setdefault(c, reason)
        for c, reason in other.protected.items():
            if c not in out.constants and c not in out.aliases and c not in out.identifiers:
                out.protected.setdefault(c, reason)
        out.warnings.extend(w for w in other.warnings if w not in out.warnings)
        return out

    def screened(self) -> list[tuple[str, str]]:
        """Every exclusion as (column, reason), for the intake report and `check`."""
        rows = [(c, r) for c, r in self.excluded().items()]
        for c, metrics in sorted(self.leaks.items()):
            for m, reason in metrics.items():
                rows.append((c, f"leaks {m}: {reason}"))
        return rows

    def suspicions(self) -> list[tuple[str, str]]:
        return [
            (c, f"suspected of leaking {m}: {reason}")
            for c, metrics in sorted(self.suspected.items()) for m, reason in metrics.items()
        ]


def analysable_columns(data: pd.DataFrame, charter: RoleCharter) -> list[str]:
    """The columns the charter's own screens leave: not an identifier, not personal, not
    protected, not forbidden. The same rule `tools.profile` applies."""
    blocked = set(charter.constraints.forbidden_features)
    return [c for c in data.columns if not gates.is_sensitive_column(c) and c not in blocked]


def _is_binary(series: pd.Series) -> bool:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    return len(numeric) == len(series.dropna()) and set(np.unique(numeric)).issubset({0, 1})


def _functional(d: pd.DataFrame, a: str, b: str) -> bool:
    """Every value of `a` maps to exactly one value of `b`."""
    return int(d.groupby(a, sort=False)[b].nunique(dropna=True).max()) == 1


def _singleton_share(d: pd.DataFrame, a: str) -> float:
    sizes = d.groupby(a, sort=False).size()
    return float(sizes[sizes == 1].sum() / max(1, len(d)))


def _rho(d: pd.DataFrame, c: str, m: str) -> float:
    """|Spearman| between two numeric columns, computed away from a shared tie block.

    Usage data is zero-inflated: when most rows are 0 in both columns, the tied ranks
    alone push Spearman towards 1 and a numerator looks like a copy of the ratio it feeds.
    So rows where both sit at their modal value are dropped first, when that block is
    large enough to matter and enough rows remain to say anything.
    """
    both_modal = (d[c] == d[c].mode().iloc[0]) & (d[m] == d[m].mode().iloc[0])
    rest = d[~both_modal]
    if both_modal.mean() >= 0.2 and len(rest) >= 10:
        d = rest
    if d[c].nunique() < 2 or d[m].nunique() < 2:
        return 0.0
    rho = d[c].corr(d[m], method="spearman")
    return abs(float(rho)) if pd.notna(rho) else 0.0


def _one(series: pd.Series) -> str:
    values = series.dropna().unique()
    return repr(values[0]) if len(values) else "null"


def derive(data: pd.DataFrame, charter: RoleCharter) -> Derived:
    out = Derived()
    n = len(data)
    if n == 0:
        return out
    columns = analysable_columns(data, charter)
    metrics = [a.metric for a in charter.accountabilities if a.metric in data.columns]
    standard = charter.evidence_standards

    # R0 — a metric that cannot move, or barely can.
    live: list[str] = []
    for m in metrics:
        series = data[m]
        if series.dropna().nunique() < 2:
            out.unmeasurable[m] = f"constant: every row is {_one(series)}"
            continue
        live.append(m)
        if _is_binary(series):
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            minority = int(min(numeric.sum(), len(numeric) - numeric.sum()))
            if minority < standard.min_sample_size:
                out.warnings.append(
                    f"metric {m!r} is nearly constant: its minority class has {minority} rows, "
                    f"below the charter minimum sample of {standard.min_sample_size}; no "
                    f"subgroup can clear the gate on it"
                )

    # R1 — constants.
    for c in columns:
        if data[c].dropna().nunique() <= 1:
            out.constants[c] = f"constant: every row is {_one(data[c])}"

    def leak(c: str, m: str, reason: str) -> None:
        out.leaks.setdefault(c, {}).setdefault(m, reason)

    # R2–R5 — each analysable column against each live metric.
    for m in live:
        m_binary = _is_binary(data[m])
        for c in columns:
            if c == m or c in out.constants:
                continue
            d = data[[c, m]].dropna()
            if len(d) < 2:
                continue
            nun_c, nun_m = d[c].nunique(), d[m].nunique()
            if nun_c < 2 or nun_m < 2:
                continue
            if pd.api.types.is_numeric_dtype(d[c]) and pd.api.types.is_numeric_dtype(d[m]):
                rho = _rho(d, c, m)
                if rho >= COPY_RHO:
                    leak(c, m, f"monotone transform of {m!r} (Spearman rho={rho:.2f})")
                    continue
                if rho >= SUSPECT_RHO:
                    out.suspected.setdefault(c, {}).setdefault(
                        m, f"nearly a monotone transform of {m!r} (Spearman rho={rho:.2f})")
            if _functional(d, c, m) and _singleton_share(d, c) <= SINGLETON_SHARE:
                leak(c, m, f"{m!r} is a function of {c!r}: every value of {c!r} maps to one value of {m!r}")
                continue
            if nun_m <= n / 2 and _functional(d, m, c) and _singleton_share(d, m) <= SINGLETON_SHARE:
                leak(c, m, f"{c!r} is a threshold or relabelling of {m!r}: every value of {m!r} maps to one value of {c!r}")
                continue
            if m_binary and nun_c <= SEPARATION_CARDINALITY:
                target = pd.to_numeric(d[m], errors="coerce")
                for value in d[c].unique():
                    inside = target[d[c] == value]
                    if inside.nunique() == 1 and int((target == inside.iloc[0]).sum()) == len(inside):
                        leak(c, m, f"{c!r} == {value!r} selects exactly the {len(inside)} rows where "
                                   f"{m!r} == {int(inside.iloc[0])}: perfect separation")
                        break

    # R6 — aliases among the non-metric columns. Equal cardinality plus a functional
    # dependency one way is a bijection, so one direction is enough.
    cand = [c for c in columns if c not in metrics and c not in out.constants
            and 2 <= data[c].nunique() <= n / 2]
    by_card: dict[int, list[str]] = {}
    for c in cand:
        by_card.setdefault(int(data[c].nunique()), []).append(c)
    for group in by_card.values():
        for i, a in enumerate(group):
            if a in out.aliases:
                continue
            for b in group[i + 1:]:
                if b in out.aliases:
                    continue
                d = data[[a, b]].dropna()
                if len(d) and d[a].nunique() == d[b].nunique() and _functional(d, a, b):
                    out.aliases[b] = a
    return out
