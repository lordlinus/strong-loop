"""
gates.py — the platform's trust boundary.

THE central invariant:

    The agent proposes. Code disposes.

An agent may write any hypothesis it likes. It cannot mint an `Evidence`, cannot choose
which test is applied, cannot see a result before committing to the test, and cannot
override a verdict. Every path from "a model had an idea" to "the platform believes
something" runs through this file.

Three layers, in order:

  1. SCREENS  — is this even allowed to be tested? (syntax sandbox, PII, charter scope)
  2. GATES    — the statistics for one SHAPE of question.
  3. VERDICT  — the charter's evidence standard, applied by code.

Adding a gate is how the platform learns to answer a new shape of question. That is the
generalisation axis, and it is the one that was missing from earlier work: a system with
exactly one gate silently supports exactly one kind of question, and therefore one role.

    Question shape                    Gate
    -------------------------------   -----------------------------
    "this subgroup converts more"     proportion_lift     (here)
    "this group differs on X"         mean_shift          (here)
    "X drives Y"                      driver_effect       (planned)
    "this intervention works"         uplift              (planned)
    "something changed"               changepoint         (planned)
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import pandas as pd

from .charter import EvidenceStandard, RoleCharter
from .stats import (
    chi2_p_1dof,
    benjamini_hochberg,
    mann_whitney_p,
    mde_lift,
    stratified_mean_difference,
    two_proportion_test,
    welch_t_test,
)
from .types import Evidence, Hypothesis, Verdict

if TYPE_CHECKING:  # derived.py imports this module; only the annotation is needed here
    from .derived import Derived

# ======================================================================================
# 1. SCREENS
# ======================================================================================

# Column names arrive in whatever convention the source system used. A screen that only
# recognises `employee_number` is a screen that a PascalCase warehouse export walks
# straight through, which is how PII reaches a prompt. So names are split into words
# first and every rule below matches WHOLE WORDS.
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Whole names that are identifiers on their own.
_ID_EXACT = {"id", "index", "uuid", "guid", "rowid", "pk"}
# A trailing word that turns whatever precedes it into an identifier.
_ID_TRAILING = {"id", "no", "num", "number", "key", "uuid", "guid"}
# Direct identifiers and contact details.
_PII_WORDS = {
    "email", "phone", "mobile", "msisdn", "address", "addr", "postcode", "zip",
    "ssn", "nric", "passport", "iban", "dob", "birthdate",
    "firstname", "lastname", "surname", "fullname",
}
# `<qualifier> name` is a person's name; `event_name` and `role_name` are not.
_NAME_QUALIFIERS = {"first", "last", "full", "sur", "given", "middle", "family", "maiden"}
# Protected attributes. Using these in a rule is a discrimination problem long before it
# is a statistical one, so they are blocked unless a charter deliberately allows them.
# Word matching is what makes `sex` safe to list here: `unisex` is one word, not two.
_PROTECTED_WORDS = {
    "race", "ethnicity", "ethnic", "religion", "religious", "disability", "disabled",
    "sex", "gender", "marital", "pregnancy", "pregnant", "nationality", "citizenship",
    "political", "orientation", "caste",
}


class ScreenError(Exception):
    """Raised when a hypothesis must not be tested at all."""


def column_words(name: str) -> list[str]:
    """Split a column name into lowercase words, whatever convention it was written in.

    `EmployeeNumber`, `employee_number`, `Employee Number` and `EMPLOYEE-NUMBER` all
    produce `['employee', 'number']`.
    """
    return [w.lower() for w in _WORDS.findall(name)]


def is_sensitive_column(name: str) -> bool:
    words = column_words(name)
    if not words:
        return False
    return (
        "_".join(words) in _ID_EXACT
        or words[-1] in _ID_TRAILING
        or bool(_PII_WORDS & set(words))
        or bool(_PROTECTED_WORDS & set(words))
        or (words[-1] == "name" and len(words) > 1 and words[-2] in _NAME_QUALIFIERS)
    )


_SQLISM = re.compile(r"(?<![\w.])(AND|OR|NOT)(?![\w.])")


def _looks_like_sql(expr: str) -> bool:
    """Detect the SQL spelling of boolean operators so the refusal can say so.

    Deliberately *detects* rather than rewrites. Mapping `AND` to `&` would silently
    change precedence — `&` binds tighter than a comparison, so `a > 1 AND b == 2` would
    quietly become `a > (1 & b) == 2` and evaluate something nobody asked for. A wrong
    answer is worse than a refusal, so the agent is told to fix it instead.
    """
    return bool(_SQLISM.search(expr)) or "&&" in expr or "||" in expr


def screen(
    expr: str, df: pd.DataFrame, charter: RoleCharter,
    derived: "Derived | None" = None, outcome: str | None = None,
) -> set[str]:
    """Parse and vet a boolean expression. Returns referenced columns or raises.

    Blocking Call/Attribute/Subscript is what stops `df.eval` from becoming an arbitrary
    code execution path — this is a sandbox, not a linter.

    `derived` carries what THIS data says may not be tested (`loop.derived`): constant
    and duplicate columns are refused outright; a column that defines `outcome` is refused
    for a hypothesis about that outcome and left alone for every other one.
    """
    if not expr or not expr.strip():
        raise ScreenError("empty expression")
    if _looks_like_sql(expr):
        raise ScreenError(
            "SQL boolean operators are not accepted. Use pandas syntax: `&` for and, "
            "`|` for or, `~` for not, with each comparison in its own parentheses — "
            "e.g. `(premium > 1000) & (status == 'active')`. "
            f"You wrote: {expr[:120]}"
        )
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ScreenError(
            f"invalid expression syntax ({exc.msg}). Use pandas operators over column "
            "names: `&` `|` `~` `==` `!=` `>=` with parenthesised comparisons, "
            "e.g. `(premium > 1000) & (status == 'active')`. Function calls and "
            "attribute access are not permitted."
        ) from exc

    forbidden = (
        ast.Call, ast.Attribute, ast.Subscript, ast.Lambda, ast.ListComp,
        ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await, ast.NamedExpr,
    )
    if bad := next((n for n in ast.walk(tree) if isinstance(n, forbidden)), None):
        raise ScreenError(f"forbidden expression syntax: {type(bad).__name__}")

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    c = charter.constraints

    if unknown := sorted(names - set(df.columns)):
        raise ScreenError(f"references unknown columns: {unknown}")
    if c.block_sensitive_columns and (hit := sorted(n for n in names if is_sensitive_column(n))):
        raise ScreenError(f"references sensitive/identifier columns: {hit}")
    if c.forbidden_features and (hit := sorted(names & set(c.forbidden_features))):
        raise ScreenError(f"references charter-forbidden columns: {hit}")
    if c.leakage_features and (hit := sorted(names & set(c.leakage_features))):
        raise ScreenError(f"references target-leaking columns: {hit}")
    if c.allowed_features is not None and (out := sorted(names - set(c.allowed_features))):
        raise ScreenError(f"references columns outside the charter's scope: {out}")
    if derived is not None:
        excluded = derived.excluded()
        if hit := sorted(n for n in names if n in excluded):
            raise ScreenError("references excluded columns — "
                              + "; ".join(f"{n}: {excluded[n]}" for n in hit))
        if outcome and (hit := [(n, derived.leak_reason(n, outcome)) for n in sorted(names)
                                if derived.leak_reason(n, outcome)]):
            raise ScreenError(
                f"circular: references columns that define the outcome {outcome!r} — "
                + "; ".join(f"{n}: {r}" for n, r in hit)
                + ". A rule built from the metric's own definition is not a driver; choose "
                  "an attribute that varies independently of the outcome."
            )
    return names


def _suspicions(names: set[str], derived: "Derived | None", outcome: str | None) -> list[str]:
    """Warnings for columns the data suggests, but cannot prove, leak the outcome."""
    if derived is None or not outcome:
        return []
    return [f"{n!r} is suspected of leaking {outcome!r}: {r}"
            for n in sorted(names) if (r := derived.suspect_reason(n, outcome))]


def _circular(inside: np.ndarray, outside: np.ndarray, outcome: str, standard: EvidenceStandard) -> None:
    """Refuse a rule that partitions the outcome exactly.

    Intake catches columns that define a metric; this catches the RULE that does — a
    compound expression, a threshold nobody listed. When every row on one side of the rule
    has the same outcome, and that side is big enough to be a finding, the rule is the
    metric's own definition and a gate would only be confirming it.
    """
    if len(inside) == 0 or len(outside) == 0:
        return
    for side, label in ((inside, "inside"), (outside, "outside")):
        clean = side[~np.isnan(side)] if side.dtype.kind == "f" else side
        if len(clean) >= standard.min_sample_size and len(np.unique(clean)) == 1:
            raise ScreenError(
                f"circular: every one of the {len(clean)} rows {label} the rule has "
                f"{outcome} == {clean[0]!r}. The rule partitions {outcome!r} exactly, so it is "
                f"the metric's own definition rather than a driver. Choose an attribute that "
                f"varies independently of the outcome."
            )


def _is_binary(values: pd.Series) -> bool:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return set(np.unique(numeric.to_numpy())).issubset({0, 1})


def _outcome(spec: dict, ctx: "GateContext", gate: str) -> pd.Series:
    """The column a hypothesis is about: the question's metric, never a substitute.

    A model whose metric is continuous will otherwise reach for any binary column so a
    binary gate accepts the spec — and then write findings about the metric it never
    tested. That happened. So `spec.target` may only restate the question's metric.
    """
    name = spec.get("target")
    if ctx.target is not None:
        if name and name != ctx.target.name:
            raise ScreenError(
                f"target must be the question's metric {ctx.target.name!r}, not {name!r}. A "
                f"hypothesis about a different outcome belongs to a different question."
            )
        return ctx.target
    if name:
        if name not in ctx.data.columns:
            raise ScreenError(f"target column {name!r} not in data")
        return ctx.data[name]
    raise ScreenError(f"{gate}: no target supplied and no accountability measure resolved")


# ======================================================================================
# 2. GATES
# ======================================================================================


@dataclass
class GateContext:
    """Everything a gate is allowed to know.

    Note what is absent: the question, the agent's rationale, the run history. A gate must
    not be talk-into-able, so it never sees the ARGUMENT for a hypothesis — only the
    hypothesis and the data.
    """

    data: pd.DataFrame
    charter: RoleCharter
    standard: EvidenceStandard
    target: pd.Series | None = None
    # What the data itself says may not be tested (`loop.derived`). None in unit tests.
    derived: "Derived | None" = None
    meta: dict[str, Any] = field(default_factory=dict)


class Gate(Protocol):
    name: str
    question_shape: str
    spec_schema: dict[str, str]

    def run(self, h: Hypothesis, ctx: GateContext) -> Evidence: ...


_REGISTRY: dict[str, Gate] = {}


def register(cls: type) -> type:
    """Class decorator. Registers an INSTANCE so `gate.run(h, ctx)` is a bound call."""
    _REGISTRY[cls.name] = cls()
    return cls


def available() -> list[dict[str, Any]]:
    """Describe the gates to the agent so it knows what it can actually ask."""
    return [
        {"name": g.name, "question_shape": g.question_shape, "spec_schema": g.spec_schema}
        for g in _REGISTRY.values()
    ]


def evaluate(h: Hypothesis, ctx: GateContext) -> Evidence:
    """Route a hypothesis to its gate and mint the Evidence. The only sanctioned path.

    Failures become Evidence rather than exceptions: a refused test is still a recorded
    fact, and an agent that never learns WHY it was refused will simply try the same thing
    again next iteration.
    """
    gate = _REGISTRY.get(h.kind)
    if gate is None:
        return Evidence(
            hypothesis_id=h.id, gate=h.kind, verdict=Verdict.REFUSED,
            refusal_reason=f"unknown gate {h.kind!r}; available: {sorted(_REGISTRY)}",
        )
    try:
        return gate.run(h, ctx)
    except ScreenError as exc:
        return Evidence(
            hypothesis_id=h.id, gate=gate.name, verdict=Verdict.REFUSED,
            refusal_reason=f"screen: {exc}",
        )
    except Exception as exc:  # a malformed spec must never kill the loop
        return Evidence(
            hypothesis_id=h.id, gate=gate.name, verdict=Verdict.REFUSED,
            refusal_reason=f"{type(exc).__name__}: {exc}",
        )


def _mask(df: pd.DataFrame, expr: str) -> pd.Series:
    return pd.Series(df.eval(expr, engine="python"), index=df.index).fillna(False).astype(bool)


@register
class ProportionLiftGate:
    """Does the subgroup selected by `where` show a higher rate of a binary `target`?"""

    name = "proportion_lift"
    question_shape = "Does subgroup `where` show a higher rate of binary `target`?"
    spec_schema = {
        "where": "REQUIRED. pandas boolean expression, parenthesised comparisons joined by & | ~ — e.g. \"(premium > 1000) & (status == 'active')\". NOT SQL: AND/OR/&&/= are refused.",
        "target": "Not settable: always the question's own metric. A claim about another outcome belongs under that outcome's question.",
    }

    def run(self, h: Hypothesis, ctx: GateContext) -> Evidence:
        where = h.spec.get("where")
        if not where:
            raise ScreenError("spec.where is required")
        df, target = ctx.data, self._target(h.spec, ctx)
        names = screen(where, df, ctx.charter, ctx.derived, outcome=target.name)

        mask = _mask(df, where)
        n, warnings = int(mask.sum()), _suspicions(names, ctx.derived, target.name)
        base = float(target.mean())

        if n == 0 or n == len(df):
            return Evidence(
                hypothesis_id=h.id, gate=self.name, verdict=Verdict.INCONCLUSIVE,
                sample_size=n, statistics={"where": where, "base_rate": base},
                warnings=["rule selects everyone or no one — it separates nothing"],
            )
        _circular(target[mask].to_numpy(), target[~mask].to_numpy(), str(target.name), ctx.standard)

        hits = int(target[mask].sum())
        rate = hits / n
        lift = rate / base if base > 0 else float("nan")
        a, b = hits, n - hits
        c = int(target.sum()) - hits
        d = len(df) - n - c
        p, test = two_proportion_test(a, b, c, d)

        # A rank test on the same split is a cheap robustness check: when the two
        # disagree, the headline usually rests on a handful of rows.
        rank_p = mann_whitney_p(
            target[mask].to_numpy(float), target[~mask].to_numpy(float)
        )
        if (p < 0.05) != (rank_p < 0.05):
            warnings.append(f"{test} p={p:.4g} vs rank p={rank_p:.4g} disagree — fragile")

        detectable = mde_lift(n, base)
        if np.isfinite(detectable) and np.isfinite(lift) and lift < detectable:
            warnings.append(
                f"lift {lift:.2f} is below the minimum detectable lift {detectable:.2f} "
                f"at n={n} — underpowered, not disproven"
            )

        return Evidence(
            hypothesis_id=h.id, gate=self.name,
            verdict=decide(n=n, p=p, effect=lift, standard=ctx.standard, warnings=warnings),
            sample_size=n,
            effect_size=round(float(lift), 4) if np.isfinite(lift) else None,
            p_value=float(p),
            statistics={
                "where": where, "target": str(target.name), "test": test,
                "subgroup_rate": round(rate, 5),
                "base_rate": round(base, 5), "coverage": round(n / len(df), 4),
                "min_detectable_lift": round(float(detectable), 4)
                if np.isfinite(detectable) else None,
                "rank_test_p": float(rank_p),
            },
            warnings=warnings,
        )

    @staticmethod
    def _target(spec: dict, ctx: GateContext) -> pd.Series:
        series = _outcome(spec, ctx, "proportion_lift")
        numeric = pd.to_numeric(series, errors="coerce").fillna(0)
        if not set(np.unique(numeric.to_numpy())).issubset({0, 1}):
            raise ScreenError(
                f"proportion_lift needs a binary target; {series.name!r} is not. "
                f"Use the mean_shift gate instead."
            )
        return numeric.astype(int).rename(series.name)


@register
class MeanShiftGate:
    """Is a continuous `measure` different inside the subgroup selected by `where`?

    The second shape, and the one that proves the registry earns its keep: "claim cost is
    higher for X" is not a conversion question, and forcing it through a lift gate is how
    a platform quietly becomes a single-use tool.
    """

    name = "mean_shift"
    question_shape = "Is the mean of `measure` different inside subgroup `where`?"
    spec_schema = {
        "where": "REQUIRED. pandas boolean expression, parenthesised comparisons joined by & | ~ — e.g. \"(premium > 1000) & (status == 'active')\". NOT SQL: AND/OR/&&/= are refused.",
        "measure": "REQUIRED. The question's own metric (a numeric column); any other column is refused.",
    }

    def run(self, h: Hypothesis, ctx: GateContext) -> Evidence:
        where, measure = h.spec.get("where"), h.spec.get("measure")
        if not where:
            raise ScreenError("spec.where is required")
        if not measure:
            raise ScreenError("spec.measure is required")
        if measure not in ctx.data.columns:
            raise ScreenError(f"measure column {measure!r} not in data")
        if ctx.target is not None and measure != ctx.target.name:
            raise ScreenError(
                f"measure must be the question's metric {ctx.target.name!r}, not {measure!r}. "
                f"A claim about a different outcome belongs to a different question."
            )
        if measure in (ctx.charter.constraints.forbidden_features or []):
            raise ScreenError(f"measure {measure!r} is forbidden by the charter")
        names = screen(where, ctx.data, ctx.charter, ctx.derived, outcome=measure)

        mask = _mask(ctx.data, where)
        values = pd.to_numeric(ctx.data[measure], errors="coerce")
        inside = values[mask].dropna().to_numpy(float)
        outside = values[~mask].dropna().to_numpy(float)
        n, warnings = len(inside), _suspicions(names, ctx.derived, measure)

        if n == 0 or len(outside) == 0:
            return Evidence(
                hypothesis_id=h.id, gate=self.name, verdict=Verdict.INCONCLUSIVE,
                sample_size=n, statistics={"where": where, "measure": measure},
                warnings=["rule selects everyone or no one — nothing to compare"],
            )
        _circular(inside, outside, measure, ctx.standard)

        p, cohens_d = welch_t_test(inside, outside)
        rank_p = mann_whitney_p(inside, outside)
        mean_in, mean_out = float(np.mean(inside)), float(np.mean(outside))
        med_in, med_out = float(np.median(inside)), float(np.median(outside))

        # Means are fragile on skewed business data (premiums, claim costs). Comparing the
        # mean shift against the median shift is the cheapest available lie detector.
        if (mean_in > mean_out) != (med_in > med_out):
            warnings.append(
                "mean and median move in opposite directions — driven by outliers, "
                "not by the subgroup"
            )
        if (p < 0.05) != (rank_p < 0.05):
            warnings.append(f"t-test p={p:.4g} vs rank p={rank_p:.4g} disagree — fragile")

        # The charter states min_effect_size on a ratio-like scale (e.g. 1.2), so put
        # Cohen's d on the same scale before gating.
        effect = 1.0 + abs(cohens_d)
        return Evidence(
            hypothesis_id=h.id, gate=self.name,
            verdict=decide(n=n, p=p, effect=effect, standard=ctx.standard, warnings=warnings),
            sample_size=n, effect_size=round(effect, 4), p_value=float(p),
            statistics={
                "where": where, "measure": measure,
                "mean_inside": round(mean_in, 4), "mean_outside": round(mean_out, 4),
                "median_inside": round(med_in, 4), "median_outside": round(med_out, 4),
                "cohens_d": round(float(cohens_d), 4), "rank_test_p": float(rank_p),
                "n_inside": n, "n_outside": len(outside),
            },
            warnings=warnings,
        )


# ======================================================================================
# 3. VERDICT
# ======================================================================================


def decide(
    *, n: int, p: float | None, effect: float | None,
    standard: EvidenceStandard, warnings: list[str],
) -> Verdict:
    """Apply the charter's evidence standard.

    INCONCLUSIVE is first-class and matters more than it looks: without it an underpowered
    test reads as REJECTED, and the agent learns the wrong lesson — it abandons a
    direction that was never actually tested.
    """
    if n < standard.min_sample_size:
        warnings.append(f"sample {n} below the charter minimum {standard.min_sample_size}")
        return Verdict.INCONCLUSIVE
    if p is None or math.isnan(p) or effect is None or math.isnan(effect):
        return Verdict.INCONCLUSIVE
    if p < standard.max_p_value and abs(effect) >= standard.min_effect_size:
        return Verdict.SUPPORTED
    return Verdict.REJECTED


def apply_multiple_testing(evidences: list[Evidence], standard: EvidenceStandard) -> list[Evidence]:
    """Correct across every test in the run, then re-gate anything that no longer holds.

    Run this before drawing conclusions, never per-hypothesis: the correction depends on
    how many tests were performed, and an autonomous loop performs a great many. At
    alpha=0.05, 200 null hypotheses yield ~10 "discoveries" by chance.
    """
    if standard.multiple_testing_correction == "none":
        return evidences

    pvals = {e.id: e.p_value for e in evidences}
    if standard.multiple_testing_correction == "bonferroni":
        m = max(1, len([v for v in pvals.values() if v is not None]))
        adjusted = {k: (min(1.0, v * m) if v is not None else None) for k, v in pvals.items()}
    else:
        adjusted = benjamini_hochberg(pvals)

    for e in evidences:
        e.adjusted_p_value = adjusted.get(e.id)
        if (
            e.verdict == Verdict.SUPPORTED
            and e.adjusted_p_value is not None
            and e.adjusted_p_value >= standard.max_p_value
        ):
            e.verdict = Verdict.REJECTED
            e.warnings.append(
                f"survived raw p={e.p_value:.4g} but failed "
                f"{standard.multiple_testing_correction} at q={e.adjusted_p_value:.4g}"
            )
    return evidences


def confidence_from(evidence: Evidence, standard: EvidenceStandard) -> float:
    """Derive confidence from evidence — never from the model's self-assessment.

    Deliberately boring: its only job is to be un-gameable.
    """
    if evidence.verdict != Verdict.SUPPORTED:
        return 0.0
    p = evidence.adjusted_p_value if evidence.adjusted_p_value is not None else evidence.p_value
    if p is None:
        return 0.0
    sig = max(0.0, min(1.0, 1.0 - p / standard.max_p_value))
    margin = max(0.0, min(1.0, (abs(evidence.effect_size or 0.0) - standard.min_effect_size)
                          / max(standard.min_effect_size, 1e-9)))
    power = max(0.0, min(1.0, evidence.sample_size / (standard.min_sample_size * 5)))
    score = 0.5 * sig + 0.3 * margin + 0.2 * power
    return round(min(1.0, score * (0.9 if evidence.warnings else 1.0)), 3)


@register
class DriverEffectGate:
    """Does `where` still associate with the outcome once `control` is held constant?

    This is a different SHAPE of question from the two subgroup gates, not a variation on
    them. `proportion_lift` asks whether a group differs. This asks whether it differs
    *because of the thing you named*, or because that thing travels with something else.

    It is the question every real root-cause investigation turns on, and in insurance it is
    usually the only question that matters:

      * an assessor with high leakage — or an assessor who is handed the complex claims?
      * a rating factor that predicts loss — or one standing in for vehicle value?
      * a channel with poor persistency — or a channel that sells to younger buyers?

    Acting on the proxy instead of the cause is not a small error. It spends the whole
    intervention budget on something that was never driving the outcome.

    Method: stratify on `control`, compute the association inside each stratum, and pool.
    A binary outcome pools odds ratios with Mantel-Haenszel; a continuous one pools
    within-stratum mean differences by inverse variance — so a role whose metric is a
    share or an amount can still be made to challenge its own findings, rather than reach
    for some other binary column so the gate will accept the spec. The verdict is decided
    on the ADJUSTED effect, never the crude one. When the two diverge the gate says so
    loudly — that divergence IS the finding.
    """

    name = "driver_effect"
    question_shape = (
        "Does subgroup `where` still shift the question's metric after stratifying on `control`?"
    )
    spec_schema = {
        "where": "REQUIRED. pandas boolean expression selecting the candidate driver, parenthesised comparisons joined by & | ~. NOT SQL.",
        "control": "REQUIRED. Column to hold constant — the suspected confound.",
        "target": "Not settable: always the question's own metric (binary or continuous).",
        "bins": "OPTIONAL. Number of quantile strata for a numeric control. Default 4.",
    }

    def run(self, h: Hypothesis, ctx: GateContext) -> Evidence:
        where = h.spec.get("where")
        control = h.spec.get("control")
        if not where:
            raise ScreenError("spec.where is required")
        if not control:
            raise ScreenError(
                "spec.control is required — this gate exists to hold a confound constant. "
                "To test a plain subgroup difference, use proportion_lift or mean_shift."
            )
        outcome = _outcome(h.spec, ctx, self.name)
        name = str(outcome.name)
        if control == name:
            raise ScreenError(f"control must not be the outcome {name!r} itself")
        names = screen(where, ctx.data, ctx.charter, ctx.derived, outcome=name)
        # The control is a column reference, so screen it as one: a confound may not be a
        # sensitive attribute, and may not be the metric's own definition either.
        names |= screen(control, ctx.data, ctx.charter, ctx.derived, outcome=name)

        df = ctx.data
        mask = _mask(df, where)
        n = int(mask.sum())
        warnings = _suspicions(names, ctx.derived, name)

        if n == 0 or n == len(df):
            return Evidence(
                hypothesis_id=h.id, gate=self.name, verdict=Verdict.INCONCLUSIVE,
                sample_size=n, statistics={"where": where, "control": control},
                warnings=["rule selects everyone or no one — it separates nothing"],
            )

        strata = self._strata(df[control], int(h.spec.get("bins", 4) or 4))
        values = pd.to_numeric(outcome, errors="coerce")
        if _is_binary(values):
            return self._binary(h, ctx, where, control, mask, values.fillna(0).astype(int), strata, warnings)
        return self._continuous(h, ctx, where, control, mask, values, strata, warnings)

    # ---- binary outcome: Mantel-Haenszel over odds ratios -------------------------
    def _binary(self, h, ctx, where, control, mask, target, strata, warnings) -> Evidence:
        df, n = ctx.data, int(mask.sum())
        _circular(target[mask].to_numpy(), target[~mask].to_numpy(), str(target.name), ctx.standard)

        crude = self._odds_ratio(
            a=int(target[mask].sum()), b=int((~target.astype(bool))[mask].sum()),
            c=int(target[~mask].sum()), d=int((~target.astype(bool))[~mask].sum()),
        )

        num = den = 0.0
        chi_num = chi_den = 0.0
        used = 0
        for level in strata.dropna().unique():
            in_stratum = (strata == level).to_numpy()
            if in_stratum.sum() < 2:
                continue
            t = target.to_numpy()[in_stratum].astype(bool)
            m = mask.to_numpy()[in_stratum]
            a = int((m & t).sum())
            b = int((m & ~t).sum())
            c = int((~m & t).sum())
            d = int((~m & ~t).sum())
            total = a + b + c + d
            if total == 0 or (a + b) == 0 or (c + d) == 0:
                continue
            used += 1
            num += a * d / total
            den += b * c / total
            expected = (a + b) * (a + c) / total
            variance = (
                (a + b) * (c + d) * (a + c) * (b + d) / (total**2 * (total - 1))
                if total > 1 else 0.0
            )
            chi_num += a - expected
            chi_den += variance

        if used < 2:
            return self._uncontrolled(h, where, control, n, used)

        adjusted = (num / den) if den > 0 else float("inf")
        chi2 = (abs(chi_num) - 0.5) ** 2 / chi_den if chi_den > 0 else 0.0
        p = chi2_p_1dof(chi2)

        # The whole reason this gate exists: compare crude to adjusted.
        explained = None
        if math.isfinite(crude) and math.isfinite(adjusted) and crude > 0:
            shrink = (crude - adjusted) / (crude - 1.0) if abs(crude - 1.0) > 1e-9 else 0.0
            explained = round(float(max(0.0, min(1.0, shrink))), 3)
            self._warn(warnings, control, explained, f"crude OR {crude:.2f} -> adjusted {adjusted:.2f}",
                       reversed_=(crude > 1.0) != (adjusted > 1.0))

        effect = float(adjusted) if math.isfinite(adjusted) else None
        return Evidence(
            hypothesis_id=h.id, gate=self.name,
            # Decided on the ADJUSTED effect. The crude one is reported for contrast only.
            verdict=decide(
                n=n, p=p, effect=effect if effect is not None else 0.0,
                standard=ctx.standard, warnings=warnings,
            ),
            sample_size=n,
            effect_size=round(effect, 4) if effect is not None else None,
            p_value=float(p),
            statistics={
                "where": where,
                "control": control,
                "target": str(target.name),
                "test": "mantel_haenszel",
                "crude_odds_ratio": round(float(crude), 4) if math.isfinite(crude) else None,
                "adjusted_odds_ratio": round(effect, 4) if effect is not None else None,
                "confound_explains_fraction": explained,
                "usable_strata": used,
                "coverage": round(n / len(df), 4),
            },
            warnings=warnings,
        )

    # ---- continuous outcome: stratified mean difference ---------------------------
    def _continuous(self, h, ctx, where, control, mask, values, strata, warnings) -> Evidence:
        df, n = ctx.data, int(mask.sum())
        name = str(values.name)
        inside = values[mask].dropna().to_numpy(float)
        outside = values[~mask].dropna().to_numpy(float)
        _circular(inside, outside, name, ctx.standard)

        _, crude_d = welch_t_test(inside, outside)
        p, adjusted_d, used = stratified_mean_difference(
            values.to_numpy(float), mask.to_numpy(), strata.astype(str).to_numpy()
        )
        if used < 2:
            return self._uncontrolled(h, where, control, n, used)

        explained = None
        if abs(crude_d) > 1e-9:
            explained = round(float(max(0.0, min(1.0, (abs(crude_d) - abs(adjusted_d)) / abs(crude_d)))), 3)
            self._warn(warnings, control, explained, f"crude d {crude_d:.2f} -> adjusted {adjusted_d:.2f}",
                       reversed_=(crude_d > 0) != (adjusted_d > 0) and abs(adjusted_d) > 1e-9)

        # Same scale MeanShiftGate reports on, so one charter threshold governs both.
        effect = 1.0 + abs(adjusted_d)
        return Evidence(
            hypothesis_id=h.id, gate=self.name,
            verdict=decide(n=n, p=p, effect=effect, standard=ctx.standard, warnings=warnings),
            sample_size=n, effect_size=round(effect, 4), p_value=float(p),
            statistics={
                "where": where,
                "control": control,
                "measure": name,
                "test": "stratified_welch",
                "crude_cohens_d": round(float(crude_d), 4),
                "adjusted_cohens_d": round(float(adjusted_d), 4),
                "confound_explains_fraction": explained,
                "usable_strata": used,
                "coverage": round(n / len(df), 4),
            },
            warnings=warnings,
        )

    def _uncontrolled(self, h, where, control, n, used) -> Evidence:
        return Evidence(
            hypothesis_id=h.id, gate=self.name, verdict=Verdict.INCONCLUSIVE,
            sample_size=n,
            statistics={"where": where, "control": control, "usable_strata": used},
            warnings=[
                f"only {used} usable stratum of {control!r}; nothing was actually "
                f"controlled for. Choose a control with more variation."
            ],
        )

    @staticmethod
    def _warn(warnings: list[str], control: str, explained: float, change: str, *, reversed_: bool) -> None:
        if explained >= 0.5:
            warnings.append(
                f"{control!r} explains ~{explained:.0%} of the crude association ({change}). "
                f"The subgroup is substantially a proxy for {control!r}; acting on it acts on the proxy."
            )
        if reversed_:
            warnings.append(
                f"direction REVERSES after controlling for {control!r} ({change}) — Simpson's "
                f"paradox. The unadjusted reading is not merely weaker, it is backwards."
            )

    @staticmethod
    def _strata(series: pd.Series, bins: int) -> pd.Series:
        """Categoricals stratify as themselves; numerics by quantile."""
        numeric = pd.to_numeric(series, errors="coerce")
        if numeric.notna().mean() > 0.9 and numeric.nunique() > bins:
            try:
                return pd.qcut(numeric, q=bins, duplicates="drop").astype(str)
            except ValueError:
                return numeric.astype(str)
        return series.astype(str)

    @staticmethod
    def _odds_ratio(a: int, b: int, c: int, d: int) -> float:
        if b == 0 or c == 0:
            a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        return (a * d) / (b * c) if (b * c) > 0 else float("inf")
