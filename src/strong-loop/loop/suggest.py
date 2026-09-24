"""suggest.py — TypeSafe reviews a mismatch `intake.pair()` could not place lexically.

`_plausible_columns` deliberately returns nothing when a metric shares no subject with
any column in the data — a wrong menu is worse than no menu, and a lexical, model-free
match is the right default because it is free and fully explainable. But a real export
that shares no vocabulary at all with the charter (a count column titled `# Pillars Met`
against a charter's `capability_coverage_pct`) would otherwise reach the human with no
help. `settle()` calls this automatically, exactly when `pair()` leaves such a gap — not
as a separate opt-in step a person has to remember to run.

TypeSafe receives one closed-set Choice per unresolved accountability, batched in one
request. It never sees row-level data or sample values: only column names, dtypes, numeric
ranges, cardinality and null share from the SAME `report.analysable_columns` set that
`pair()` decided is allowed for this charter. The returned probabilities rank that list;
`none_of_the_above` lets it decline rather than force a match. The result still goes
through `intake.resolve()` — the same picks-only, human-signs gate as an unaided lexical
clarification. TypeSafe proposes; it never applies, and it never mints evidence.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from . import models
from .charter import RoleCharter
from .derived import Derived, analysable_columns
from .intake import Clarification, IntakeReport, NOTHING_MEASURES_IT
from .tools import profile

_LOG = logging.getLogger(__name__)
_NONE = "none_of_the_above"
_MAX_COLUMNS = 254  # Choice supports 255 options; one is reserved for `_NONE`.

# Semantic screens: what only the NAMES can tell. Thresholds agreed 2026-09-22.
LEAK_BLOCK = 0.70      # P(copy / component / consequence of the metric) at which a column is refused for it
LEAK_SUSPECT = 0.35    # ... at which evidence using it carries a warning
IDENTIFIER_BLOCK = 0.70
# The word list in `gates.is_sensitive_column` knows `gender`; it cannot know that
# `SeniorCitizen` is age. Same bands as leakage: refuse high, warn in the middle.
PROTECTED_BLOCK = 0.70
PROTECTED_SUSPECT = 0.35
_MAX_QUESTIONS = 400   # one request; beyond this the code tier stands alone
_SCREEN_CACHE: dict[tuple[str, str], Derived] = {}


EXISTS_MIN = 0.35   # below this, TypeSafe itself says nothing here measures the metric


def unresolved(report: IntakeReport) -> list[str]:
    """Accountability ids whose metric is not a column of this data — every one of them,
    whether or not lexical matching found a candidate. Seen live: the lexical pass offered
    two weak token columns for a hosted-agent adoption flag and the column that plausibly
    measured it was never on the menu, because the review only ran on silence."""
    return [a.id for a in report.accountabilities if a.status == "missing"]


def _brief_columns(data: pd.DataFrame, charter: RoleCharter, columns: list[str],
                    labels: dict[str, str]) -> dict[str, Any]:
    """Schema-level context only; no row value is sent to the external API."""
    cols = profile(data, charter)["columns"]
    allowed = {"dtype", "null_share", "n_unique", "range", "median"}
    return {
        name: {
            "original_header": labels.get(name, name),
            **{key: value for key, value in cols.get(name, {}).items() if key in allowed},
        }
        for name in columns
    }


async def _ask(
    columns: dict[str, Any],
    accountabilities: list[dict[str, str]],
    model: str | None,
    glossary: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Ask every mapping question in one TypeSafe request.

    Kept as one seam so tests replace the network without faking the SDK.
    """
    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, NoulCriteria

    models.load_env()
    criteria = {
        name: (
            f"Existing dataset column {name!r}. Metadata: {details!r}. Choose it only "
            "when it plausibly measures the accountability's target metric."
        )
        for name, details in columns.items()
    }
    criteria[_NONE] = (
        "No available column plausibly measures this accountability's target metric. "
        "Prefer this over a weak semantic guess."
    )
    questions: dict[str, Any] = {}
    for acc in accountabilities:
        questions[acc["id"]] = Choice(
            instructions=(
                f"Which available dataset column best measures the target metric "
                f"{acc['metric']!r} for this accountability: {acc['statement']!r}? "
                "Read the terms as the role's glossary defines them."
            ),
            criteria=criteria,
        )
        # The companion the ranking needs: a 200-way Choice dilutes `none_of_the_above`,
        # so "is there an answer at all" is asked on its own.
        questions[f"{acc['id']}|exists"] = Noul(
            instructions=(
                f"Does ANY available dataset column plausibly measure the target metric "
                f"{acc['metric']!r} ({acc['statement']!r}), read with the glossary?"
            ),
            criteria=NoulCriteria(
                true="At least one column is the metric, a rescaling of it, or a direct measure of the same quantity.",
                false="Nothing listed measures this; the closest columns are related but not this quantity.",
            ),
        )
    async with AsyncTypeSafeClient(
        model=model or os.environ.get("TYPESAFE_DEFAULT_MODEL"),
        timeout=30.0,
    ) as client:
        response = await client.system_one(
            state={"available_columns": columns, "glossary": glossary or {}},
            questions=questions,
        )
    return {
        aid: {
            "choice": answer.choice,
            "confidence": float(answer.confidence),
            "probabilities": dict(answer.probabilities),
            "exists": float(response.nouls[f"{aid}|exists"].noul) if f"{aid}|exists" in response.nouls else 1.0,
        }
        for aid, answer in response.choices.items()
    }


async def suggest_mappings(charter: RoleCharter, data: pd.DataFrame, report: IntakeReport,
                            column_labels: dict[str, str] | None = None,
                            model: str | None = None) -> list[Clarification]:
    """One TypeSafe call, ranking `report.analysable_columns` against each unresolved
    accountability. Returns new `Clarification`s only; never mutates `report` or
    `charter`. Every candidate is checked against the real, already-screened column
    list before it is returned, so a hallucinated or forbidden name can never reach a
    human as an option — the model narrows a menu, the same closed-list rule as any
    other clarification still applies.
    """
    targets = unresolved(report)
    if not targets:
        return []

    accs = {a.id: a for a in charter.accountabilities}
    columns = _brief_columns(data, charter, report.analysable_columns, column_labels or {})
    if len(columns) > _MAX_COLUMNS:
        _LOG.warning(
            "TypeSafe intake review skipped: %d analysable columns exceeds the closed-set "
            "Choice limit of %d",
            len(columns),
            _MAX_COLUMNS,
        )
        return []
    accountabilities = [
        {"id": aid, "statement": accs[aid].statement, "metric": accs[aid].metric}
        for aid in targets
    ]
    try:
        answers = await _ask(columns, accountabilities, model, dict(charter.glossary))
    except Exception as exc:
        _LOG.warning("TypeSafe intake review unavailable: %s: %s", type(exc).__name__, exc)
        return []
    if not isinstance(answers, dict):
        return []

    valid = set(report.analysable_columns)
    out: list[Clarification] = []
    for aid in targets:
        answer = answers.get(aid) or {}
        probabilities = answer.get("probabilities") or {}
        if not isinstance(probabilities, dict) or answer.get("choice") == _NONE:
            continue
        exists = answer.get("exists", 1.0)
        if isinstance(exists, (int, float)) and exists < EXISTS_MIN:
            continue
        ranked = sorted(
            (
                (name, float(probability))
                for name, probability in probabilities.items()
                if name in valid and isinstance(probability, (int, float))
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        candidates = [name for name, _probability in ranked[:5]]
        if not candidates:
            continue
        acc = accs[aid]
        confidence = float(answer.get("confidence") or 0.0)
        out.append(Clarification(
            accountability_id=aid, metric=acc.metric,
            question=(f"This role is accountable for {acc.metric!r}, which is not in this "
                      f"data. TypeSafe ranked the available columns as possible matches "
                      f"(confidence {confidence:.2f}) — which one measures it?"),
            why=(f"'{acc.statement}' cannot be tested until this resolves. No column name "
                 f"shared vocabulary with {acc.metric!r}, so this menu is ranked from a "
                 f"closed-set TypeSafe Choice rather than lexical matching; verify it "
                 f"before picking. Choosing "
                 f"{NOTHING_MEASURES_IT!r} leaves it out of this run, which is a real "
                 f"result and better than a wrong guess."),
            candidates=candidates[:5] + [NOTHING_MEASURES_IT],
        ))
    return out


async def review(charter: RoleCharter, data: pd.DataFrame, report: IntakeReport,
                  column_labels: dict[str, str] | None = None, model: str | None = None) -> IntakeReport:
    """`report`, with a model's clarifications folded in for whatever `pair()` left
    genuinely unresolved. This is the automatic step `settle()` takes on every mismatch —
    not a separate action a person has to remember to trigger. A clean pairing, or one
    fully resolved by lexical matching, calls the model zero times: `unresolved()` is
    empty and `suggest_mappings` returns immediately.

    Never raises: a misconfigured or unreachable API must degrade to the lexical-only
    report, not block intake. Where lexical matching already offered a menu, the model's
    candidates are appended after it (shared vocabulary stays the most legible reason to
    rank first) and the opt-out stays last; the menu remains a closed list.
    """
    try:
        extra = await suggest_mappings(charter, data, report, column_labels, model)
    except Exception as exc:  # an API failure must never block intake reaching the human
        _LOG.warning("TypeSafe intake review failed: %s: %s", type(exc).__name__, exc)
        return report
    if not extra:
        return report
    by_id = {c.accountability_id: c for c in extra}
    merged: list[Clarification] = []
    for c in report.clarifications:
        more = by_id.pop(c.accountability_id, None)
        if more is None:
            merged.append(c)
            continue
        picks = [x for x in c.candidates if x != NOTHING_MEASURES_IT]
        picks += [x for x in more.candidates if x not in picks and x != NOTHING_MEASURES_IT]
        merged.append(c.model_copy(update={
            "candidates": picks + [NOTHING_MEASURES_IT],
            "why": c.why + " Candidates after the lexical ones were ranked by a closed-set "
                   "TypeSafe Choice over the same screened columns; verify before picking.",
        }))
    merged.extend(by_id.values())
    return report.model_copy(update={"clarifications": merged})


# ======================================================================================
# Semantic screens — the second tier over `loop.derived`
# ======================================================================================

async def _ask_nouls(state: dict[str, Any], questions: dict[str, Any], model: str | None) -> dict[str, float]:
    """One TypeSafe request answering every Noul at once; returns key -> P(yes).

    The seam tests replace, like `_ask`.
    """
    from typesafe_sdk import AsyncTypeSafeClient

    models.load_env()
    async with AsyncTypeSafeClient(model=model or os.environ.get("TYPESAFE_DEFAULT_MODEL"),
                                   timeout=60.0) as client:
        response = await client.system_one(state=state, questions=questions)
    return {key: float(answer.noul) for key, answer in response.nouls.items()}


def _schema_key(charter: RoleCharter, data: pd.DataFrame) -> tuple[str, str]:
    schema = ";".join(f"{c}:{data[c].dtype}" for c in data.columns)
    return charter.fingerprint(), schema


async def semantic_screens(charter: RoleCharter, data: pd.DataFrame, derived: Derived,
                           column_labels: dict[str, str] | None = None,
                           model: str | None = None) -> Derived:
    """What column NAMES say that the data cannot: `agent_tokens` is the numerator of
    `agent_token_pct` (rho 0.92, no functional dependency — the code tier is blind to it);
    `TPID` is an identifier the word list does not know.

    One request, schema metadata only (`_brief_columns`: headers, dtype, null share,
    cardinality, range, median — never a row). Per analysable column and live metric one
    Noul "copy, rescaling, component or post-outcome consequence?", plus one Noul
    "record identifier?" per column and, while the charter blocks sensitive columns, one
    "protected attribute or proxy?" per column. P >= LEAK_BLOCK refuses the column for THAT metric;
    LEAK_SUSPECT <= P < LEAK_BLOCK warns. Returns the semantic tier alone; the caller
    merges it under the code tier, which wins wherever both have an opinion. Missing key,
    API error or an oversize schema all return an empty tier: the loop never depends on
    this call.
    """
    key = _schema_key(charter, data)
    if key in _SCREEN_CACHE:
        return _SCREEN_CACHE[key]
    excluded = derived.excluded()
    columns = [c for c in analysable_columns(data, charter) if c not in excluded]
    metrics = [a.metric for a in charter.accountabilities
               if a.metric in data.columns and a.metric not in derived.unmeasurable]
    out = Derived()
    if not columns or not metrics:
        return out
    from typesafe_sdk import Noul, NoulCriteria

    brief = _brief_columns(data, charter, columns, column_labels or {})
    accs = {a.metric: a for a in charter.accountabilities}
    questions: dict[str, Any] = {}
    for c in columns:
        header = brief[c].get("original_header", c)
        for m in metrics:
            if c == m:
                continue
            questions[f"{c}|leaks|{m}"] = Noul(
                instructions=(
                    f"Judging from names and metadata only: is column {c!r} (header {header!r}) "
                    f"a direct copy, rescaling, component, or post-outcome consequence of the "
                    f"metric {m!r} ({accs[m].statement})?"
                ),
                criteria=NoulCriteria(
                    true=("Testing this column against that metric would be circular: it is the "
                          "metric under another name, a rescaled version, an input the metric is "
                          "computed from, or something that can only exist after the outcome."),
                    false=("An independent attribute of the record that could plausibly drive "
                           "the metric without being part of its definition."),
                ),
            )
        questions[f"{c}|identifier"] = Noul(
            instructions=f"Is column {c!r} (header {header!r}) a record identifier rather than an attribute?"
        )
        if charter.constraints.block_sensitive_columns:
            questions[f"{c}|protected"] = Noul(
                instructions=(
                    f"Judging from names and metadata only: is column {c!r} (header {header!r}) "
                    f"a protected personal attribute or a direct proxy for one?"
                ),
                criteria=NoulCriteria(
                    true=("It records, or stands in for, a person's age, sex or gender, race or "
                          "ethnicity, religion, disability, marital or family status, pregnancy, "
                          "nationality or sexual orientation — e.g. a senior-citizen flag, a birth "
                          "year, a title such as Mr/Mrs."),
                    false=("An attribute of the record's behaviour, product, account or context "
                           "that says nothing about a protected characteristic."),
                ),
            )
    if len(questions) > _MAX_QUESTIONS:
        _LOG.warning("TypeSafe semantic screens skipped: %d questions exceeds %d", len(questions), _MAX_QUESTIONS)
        return out
    try:
        answers = await _ask_nouls(
            {"columns": brief,
             "metrics": [{"metric": m, "statement": accs[m].statement} for m in metrics],
             "glossary": dict(charter.glossary)},
            questions, model,
        )
    except Exception as exc:
        _LOG.warning("TypeSafe semantic screens unavailable: %s: %s", type(exc).__name__, exc)
        return out
    if not isinstance(answers, dict):
        return out
    valid = set(columns)
    for key_, p in answers.items():
        if not isinstance(p, (int, float)):
            continue
        parts = str(key_).split("|")
        if parts[0] not in valid:
            continue
        if len(parts) == 3 and parts[1] == "leaks" and parts[2] in metrics:
            c, m = parts[0], parts[2]
            if p >= LEAK_BLOCK:
                out.leaks.setdefault(c, {})[m] = (
                    f"semantic: P={p:.2f} that it is a copy, component or consequence of {m!r} "
                    f"(TypeSafe, from column names and metadata only)")
            elif p >= LEAK_SUSPECT:
                out.suspected.setdefault(c, {})[m] = (
                    f"semantic: P={p:.2f} that it is a copy, component or consequence of {m!r}")
        elif len(parts) == 2 and parts[1] == "identifier" and p >= IDENTIFIER_BLOCK:
            out.identifiers[parts[0]] = f"identifier (semantic: P={p:.2f}, TypeSafe)"
        elif len(parts) == 2 and parts[1] == "protected":
            if p >= PROTECTED_BLOCK:
                out.protected[parts[0]] = (f"protected attribute or proxy (semantic: P={p:.2f}, "
                                           f"TypeSafe, from column names and metadata only)")
            elif p >= PROTECTED_SUSPECT:
                out.warnings.append(
                    f"column {parts[0]!r} may be a protected attribute or proxy (semantic: "
                    f"P={p:.2f}); it stays testable — review any finding or action that uses it")
    _SCREEN_CACHE[key] = out
    return out



# ======================================================================================
# Claim screens — the model's prose read against the record it cites
# ======================================================================================
#
# The gate scores the claim the model SUBMITTED; nothing checked what it then WROTE. A
# headline, an interpretation and a recommendation are the only model text that reaches
# `report.json` unexamined, and SYSTEM rule 9 ("an action, not a study") was a prompt
# instruction with no mechanism behind it. The frontier model keeps the reasoning; this
# asks the small closed questions about its prose (citation_check + llm_guardrails
# cookbooks). State is ledger records only — never a row. Refuse only when confident,
# warn in the middle, and an unavailable API records exactly as before.

CLAIM_REFUSE = 0.80
CLAIM_WARN = 0.50


def _ask_sync(state: dict[str, Any], questions: dict[str, Any], model: str | None) -> dict[str, Any]:
    """One synchronous TypeSafe request (the tools are sync). Returns key -> P(yes) for a
    Noul, key -> {option: P} for a Choice. The seam tests replace."""
    from typesafe_sdk import TypeSafeClient

    models.load_env()
    with TypeSafeClient(model=model or os.environ.get("TYPESAFE_DEFAULT_MODEL"), timeout=15.0) as client:
        response = client.system_one(state=state, questions=questions)
    out: dict[str, Any] = {key: float(a.noul) for key, a in response.nouls.items()}
    out.update({key: dict(a.probabilities) for key, a in response.choices.items()})
    return out


def _screen(state: dict[str, Any], questions: dict[str, Any], model: str | None) -> dict[str, Any]:
    try:
        answers = _ask_sync(state, questions, model)
    except Exception as exc:
        _LOG.warning("TypeSafe claim screen unavailable: %s: %s", type(exc).__name__, exc)
        return {}
    return answers if isinstance(answers, dict) else {}


def _p(answers: dict[str, Any], key: str, option: str | None = None) -> float | None:
    value = answers.get(key)
    if option is not None:
        value = value.get(option) if isinstance(value, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _record(evidence: Any, hypothesis: Any) -> dict[str, Any]:
    return {
        "evidence_id": evidence.id, "gate": evidence.gate, "verdict": evidence.verdict.value,
        "effect_size": evidence.effect_size, "p_value": evidence.p_value,
        "sample_size": evidence.sample_size, "statistics": evidence.statistics,
        "warnings": evidence.warnings,
        "tested_claim": getattr(hypothesis, "statement", ""),
        "tested_spec": getattr(hypothesis, "spec", {}),
    }


def review_finding(evidence: Any, hypothesis: Any, accountability: Any, headline: str,
                   interpretation: str, model: str | None = None) -> tuple[str | None, list[str]]:
    """(refusal reason or None, warnings) for a finding's prose against its evidence."""
    from typesafe_sdk import Choice, Noul, NoulCriteria

    state = {
        "evidence": _record(evidence, hypothesis),
        "accountability": ({"metric": accountability.metric, "direction": accountability.direction,
                            "statement": accountability.statement} if accountability else {}),
        "claim": {"headline": headline, "interpretation": interpretation},
    }
    answers = _screen(state, {
        "support": Choice(
            instructions=("Read the claim against the evidence record only. Do its group, "
                          "metric, direction and size agree with the statistics?"),
            criteria={
                "supports": "The claim says what the statistics show, about the group and metric tested.",
                "contradicts": ("The claim states a direction, size, group or metric the statistics "
                                "contradict."),
                "says_nothing": ("The claim is about something the record neither supports nor "
                                 "contradicts: another group, another metric, or a mechanism."),
            },
        ),
        "causal": Noul(
            instructions=("Does the claim say the group or attribute CAUSES the change in the "
                          "metric, rather than that it is associated with it?"),
            criteria=NoulCriteria(
                true="It asserts cause and effect: produces, results in, because of, will change.",
                false="It reports an association, difference or lift between groups.",
            ),
        ),
    }, model)
    contradicts = _p(answers, "support", "contradicts")
    supports = _p(answers, "support", "supports")
    if contradicts is not None and contradicts >= CLAIM_REFUSE:
        return (f"the text contradicts {evidence.id} (TypeSafe P={contradicts:.2f}): the record "
                f"shows effect_size={evidence.effect_size}, p={evidence.p_value}, "
                f"n={evidence.sample_size}. Rewrite the headline and interpretation to say what "
                f"the statistics show."), []
    warnings: list[str] = []
    if supports is not None and supports < CLAIM_WARN:
        warnings.append(f"text may not match its evidence (TypeSafe: supports P={supports:.2f})")
    if (causal := _p(answers, "causal")) is not None and causal >= CLAIM_WARN:
        warnings.append(f"text claims causation (TypeSafe P={causal:.2f}); the gate measured an association")
    return None, warnings


def review_action(recommendation: str, expected_effect: str, action_type: str,
                  right_description: str, where: str, findings: list[Any],
                  model: str | None = None) -> tuple[str | None, list[str]]:
    """(refusal reason or None, warnings) for an action's recommendation."""
    from typesafe_sdk import Noul, NoulCriteria

    state = {
        "action": {"type": action_type, "permitted_as": right_description, "group_rule": where,
                   "recommendation": recommendation, "expected_effect": expected_effect},
        "findings": [{"headline": f.headline, "interpretation": f.interpretation} for f in findings],
    }
    answers = _screen(state, {
        "study": Noul(
            instructions=("Does the recommendation describe further analysis, research, "
                          "monitoring or a study, rather than something a named person does to "
                          "the group next week?"),
            criteria=NoulCriteria(
                true="It asks for more investigation, a deep-dive, a review of data, or a pilot study.",
                false="It says what to do to the group: send, route, change, offer, contact, approve.",
            ),
        ),
        "off_evidence": Noul(
            instructions=("Does the recommendation act on a different group, or aim at a "
                          "different outcome, than the findings it cites?"),
        ),
    }, model)
    study = _p(answers, "study")
    if study is not None and study >= CLAIM_REFUSE:
        return (f"the recommendation describes a study, not an action (TypeSafe P={study:.2f}). "
                f"Say what a person does to the group {where!r} — the thing "
                f"{action_type!r} permits."), []
    warnings: list[str] = []
    if study is not None and study >= CLAIM_WARN:
        warnings.append(f"recommendation may describe a study rather than an action (TypeSafe P={study:.2f})")
    if (off := _p(answers, "off_evidence")) is not None and off >= CLAIM_WARN:
        warnings.append(f"recommendation may reach beyond its findings (TypeSafe P={off:.2f})")
    return None, warnings
