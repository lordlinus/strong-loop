"""intake.py — the join between a charter and a dataset, settled before any model runs.

A charter is written once and then meets datasets nobody had when it was written. The
interesting ambiguity is therefore not inside the charter — `validate_charter_shape`
handles that — but at the *pairing*: a metric that named a warehouse column last quarter
resolves to nothing in the CSV a person just dropped on the page.

`pair()` answers, without a model and without writing anything:

  - which columns the loop may analyse, and which it has screened out and why
  - which accountabilities this data can measure, and for those it cannot, the closed
    list of columns a human might have meant — or no list at all
  - the questions a run would inherit
  - that the gate and the screens actually bite on THIS data

`resolve()` applies a human's answers. An answer is only ever a pick from the list
`pair()` offered — free text would reintroduce, with less scrutiny, exactly the guess the
platform declined to make. Answering un-signs the charter: remapping what an
accountability measures changes what the role is answerable for, and a signature that
survived that would attest to a document nobody read. The caller signs again, by name.

No domain vocabulary here. The two shipped charters share none, and both must pass.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from . import gates
from .charter import RoleCharter
from .derived import Derived, derive
from .questions import questions_from
from .tools import profile
from .types import Hypothesis

# The answer that always has to be available: "none of these". `resolve` treats it as a
# deliberate decision to leave the accountability out of this run rather than as a
# column name.
NOTHING_MEASURES_IT = "(nothing in this data measures it)"


_WORD = gates._WORDS  # same camelCase/snake_case/space splitter the screens and stemmer use


def normalize_column_name(name: str) -> str:
    """A raw header turned into a safe python identifier.

    Gate expressions are parsed as Python (`screen()` walks `ast.Name` nodes) and a
    metric can only ever be matched, remapped onto, or later queried as one, so any
    column that is not already `str.isidentifier()` — spaces, `#`, `&`, a leading digit,
    a unit suffix — is unusable no matter how good the semantic match is. This is the
    one normalization: split on the same word boundaries `column_words` already uses,
    rejoin snake_case, and prefix a bare leading digit.
    """
    words = gates.column_words(name)
    slug = "_".join(words) or "column"
    return f"c_{slug}" if slug[0].isdigit() else slug


def normalize_columns(data: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Rename only the columns that are not already valid identifiers.

    An already-clean header (`JobSatisfaction`, `Attrition`, `attrition_flag`) is left
    exactly as the charter and any shipped data agree on it today — this must not
    disturb an existing working pairing. Returns the frame (renamed only if needed) and
    a `{new_name: original_header}` map covering just the columns that changed, so a
    reviewer can still recognise their own export in the report. Values are then read
    by `normalize_values`; what it changed is left in `data.attrs["recoded"]`.
    """
    seen = set(map(str, data.columns))
    renames: dict[str, str] = {}
    labels: dict[str, str] = {}
    for col in data.columns:
        name = str(col)
        if name.isidentifier():
            continue
        base = normalize_column_name(name)
        safe, n = base, 1
        while safe in seen:
            n += 1
            safe = f"{base}_{n}"
        renames[col] = safe
        labels[safe] = name
        seen.add(safe)
    data, recoded = normalize_values(data.rename(columns=renames) if renames else data)
    data = data.copy(deep=False)      # never write attrs onto the caller's frame
    data.attrs["recoded"] = recoded   # `pair()` reports it; attrs survive rename and copy
    return data, labels


# Spellings of a yes/no answer that mean one thing in any export. A two-valued text column
# outside these (`Gold`/`Silver`, `M`/`F`) has no safe 0/1 reading, so it stays text and
# `pair()` says so.
_YES = {"yes", "y", "true", "t"}
_NO = {"no", "n", "false", "f"}
_CODE = re.compile(r"^[+-]?0\d")   # a leading zero is a code (postcode, account), not a quantity


def normalize_values(data: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Read the two text encodings of a number every export uses as the number.

    Without this, `pd.to_numeric(errors="coerce")` downstream turns a `Yes`/`No` outcome
    into all-missing, then all-zero: seen on a churn export, where the metric was reported
    unmeasurable and the gate probe claimed every row had `Churn == 0`. And a numeric column
    with one blank cell arrives as text, which blinds every numeric screen in `derived`.

    Only unambiguous readings are applied: every non-blank value a yes/no spelling with
    both answers present, or every non-blank value a number with no leading-zero code.
    Blank cells become missing. Returns the frame and `{column: how it was read}`, so the
    person ratifying intake sees what changed.
    """
    out = data
    recoded: dict[str, str] = {}
    for col in data.columns:
        series = data[col]
        if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            continue
        text = series.astype("string").str.strip()
        present = text[text.notna() & (text != "")]
        if present.empty:
            continue
        lower = present.str.lower()
        values = set(lower.unique())
        blanks = int((text == "").fillna(False).sum())
        blank_note = f"; {blanks} blank cell(s) read as missing" if blanks else ""
        if values <= _YES | _NO and values & _YES and values & _NO:
            mapping = {**{w: 1.0 for w in _YES}, **{w: 0.0 for w in _NO}}
            numeric = text.str.lower().map(mapping).astype("float64")
            yes = sorted(set(present[lower.isin(_YES)]))
            no = sorted(set(present[lower.isin(_NO)]))
            out = out if out is not data else data.copy()
            out[col] = numeric.astype("int64") if numeric.notna().all() else numeric
            recoded[col] = f"{'/'.join(yes)} read as 1, {'/'.join(no)} as 0{blank_note}"
            continue
        if present.str.match(_CODE).any():
            continue
        parsed = pd.to_numeric(present.astype(object), errors="coerce")
        if parsed.notna().all():
            out = out if out is not data else data.copy()
            out[col] = pd.to_numeric(text.where(text != "").astype(object), errors="coerce")
            recoded[col] = f"numbers stored as text, read as numbers{blank_note}"
    return out, recoded


class Screened(BaseModel):
    column: str
    reason: str


class Clarification(BaseModel):
    """A question whose answer has exactly one legitimate place to land: the metric of
    the named accountability, chosen from a closed list."""

    accountability_id: str
    metric: str
    question: str
    why: str
    candidates: list[str]   # ranked, opt-out always last


class AccountabilityStatus(BaseModel):
    id: str
    statement: str
    metric: str
    status: str             # "ok" | "missing" | "unmeasurable" (present, but not numeric, 0/1, or constant)
    measure: dict[str, Any] = Field(default_factory=dict)   # today's level, when present


class Probe(BaseModel):
    """One hypothesis pushed through the real gate, so a broken engine fails here rather
    than in iteration three."""

    gate: str
    where: str
    verdict: str
    reason: str | None = None


class IntakeReport(BaseModel):
    role: str
    version: int
    status: str
    content_hash: str | None
    rows: int
    analysable_columns: list[str]
    leakage_columns: list[str]
    column_labels: dict[str, str] = Field(default_factory=dict)  # normalized -> original header
    recoded: dict[str, str] = Field(default_factory=dict)        # column -> how its text was read
    screened: list[Screened]
    # What the data itself rules out (`loop.derived`): columns refused for a metric they
    # define, and columns the run will never be offered. Reasons name the metric.
    derived: list[Screened] = Field(default_factory=list)
    suspected: list[Screened] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    accountabilities: list[AccountabilityStatus]
    clarifications: list[Clarification]
    questions: list[str]
    withheld: list[str] = Field(default_factory=list)   # questions a constant metric took out of this run
    gate_probe: Probe | None
    screen_probe: Probe | None
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------------------
# pair
# --------------------------------------------------------------------------------------


def pair(charter: RoleCharter, data: pd.DataFrame, column_labels: dict[str, str] | None = None,
         derived: Derived | None = None) -> IntakeReport:
    if derived is None:
        derived = derive(data, charter)   # the code tier; `settle()` adds the semantic one
    prof = profile(data, charter, derived)
    forbidden = set(charter.constraints.forbidden_features)
    dropped = derived.excluded()
    screened = [
        Screened(column=c, reason="forbidden by charter" if c in forbidden
                 else dropped.get(c) or "identifier, personal data or protected attribute")
        for c in prof["excluded_columns"]
    ]
    analysable = list(prof["columns"])

    statuses: list[AccountabilityStatus] = []
    clarifications: list[Clarification] = []
    problems: list[str] = []
    for acc in charter.accountabilities:
        present = acc.metric in data.columns
        measure = prof["accountability_measures"].get(acc.metric, {}) if present else {}
        if present and acc.metric in derived.unmeasurable:
            # A metric with one value cannot move. Its question is withheld, not the run.
            statuses.append(AccountabilityStatus(id=acc.id, statement=acc.statement,
                                                 metric=acc.metric, status="unmeasurable",
                                                 measure=measure))
            continue
        measurable = "kind" in measure
        statuses.append(AccountabilityStatus(
            id=acc.id, statement=acc.statement, metric=acc.metric,
            status="ok" if measurable else "unmeasurable" if present else "missing",
            measure=measure,
        ))
        if measurable:
            continue
        if present:
            # A Yes/No or free-text column is in the data but no gate can test it. That
            # is a source-side fix (derive a 0/1 column), not a remap, so no menu.
            values = ", ".join(map(str, data[acc.metric].dropna().unique()[:4]))
            problems.append(f"accountability {acc.id!r} metric {acc.metric!r} is not numeric or 0/1 "
                            f"(values like: {values}); derive a 0/1 column in the data and target that")
            continue
        problems.append(f"accountability {acc.id!r} metric {acc.metric!r} is not a column of this data")
        candidates = _plausible_columns(acc.metric, analysable)
        if not candidates:
            # Nothing here plausibly measures it. A menu of wrong answers is worse than
            # silence: the reviewer most likely to accept one is the one who trusts the
            # platform to have narrowed sensibly. Report the mismatch as a mismatch.
            continue
        clarifications.append(Clarification(
            accountability_id=acc.id, metric=acc.metric,
            question=(f"This role is accountable for {acc.metric!r}, which is not in this "
                      f"data. Which column measures it?"),
            why=(f"'{acc.statement}' cannot be tested until this resolves. Choosing "
                 f"{NOTHING_MEASURES_IT!r} leaves it out of this run, which is a real "
                 f"result and better than a near-miss."),
            candidates=candidates + [NOTHING_MEASURES_IT],
        ))

    if not charter.accountabilities:
        problems.append("charter has no accountabilities; there is nothing to run")
    elif not any(s.status == "ok" for s in statuses) and not clarifications:
        problems.append("no accountability can be measured in this data: "
                        + "; ".join(f"{s.metric} is {s.status}" for s in statuses))

    questions = questions_from(charter, derived)
    # Only a metric a gate can read: probing a text metric reports a coerced verdict on
    # values that are not there, which reads as a real result.
    target_col = next((s.metric for s in statuses if s.status == "ok"), None)
    ctx = gates.GateContext(
        data=data, charter=charter, standard=charter.evidence_standards,
        target=data[target_col] if target_col else None, derived=derived,
    )
    qid = questions[0].id if questions else "q_probe"

    gate_probe = None
    where = _default_probe(data, charter, derived)
    if where and target_col:
        binary = set(pd.unique(pd.to_numeric(data[target_col], errors="coerce").dropna())).issubset({0, 1})
        kind = "proportion_lift" if binary else "mean_shift"
        spec = {"where": where} if binary else {"where": where, "measure": target_col}
        e = gates.evaluate(Hypothesis(question_id=qid, statement=f"probe: {where}", kind=kind,
                                      spec=spec, rationale="intake smoke test"), ctx)
        gate_probe = Probe(gate=kind, where=where, verdict=e.verdict.value, reason=e.refusal_reason)
        if e.verdict.value == "REFUSED":
            problems.append(f"gate probe refused: {e.refusal_reason}")

    # Prove the screens bite, on a real identifier from THIS dataset. Assert on the
    # reason, not just the verdict: an internal error is also REFUSED.
    screen_probe = None
    sensitive = [c for c in data.columns if gates.is_sensitive_column(c)]
    if sensitive and target_col:
        col = sensitive[0]
        e = gates.evaluate(Hypothesis(question_id=qid, statement="probe: sensitive column",
                                      kind="proportion_lift", spec={"where": f"{col} == {col}"}), ctx)
        screen_probe = Probe(gate="proportion_lift", where=f"{col} == {col}",
                             verdict=e.verdict.value, reason=e.refusal_reason)
        if e.verdict.value != "REFUSED" or "sensitive" not in (e.refusal_reason or ""):
            problems.append(f"screens did not block identifier column {col!r}")

    return IntakeReport(
        role=charter.role, version=charter.version, status=charter.status,
        content_hash=charter.content_hash, rows=int(prof["rows"]),
        analysable_columns=analysable, leakage_columns=list(prof["leakage_columns"]),
        column_labels=dict(column_labels or {}),
        recoded=dict(data.attrs.get("recoded") or {}),
        screened=screened,
        derived=[Screened(column=c, reason=r) for c, r in derived.screened() if c not in dropped],
        suspected=[Screened(column=c, reason=r) for c, r in derived.suspicions()],
        warnings=list(derived.warnings),
        accountabilities=statuses, clarifications=clarifications,
        questions=[q.text for q in questions if q.status == "open"],
        withheld=[q.text for q in questions if q.status == "withheld"],
        gate_probe=gate_probe, screen_probe=screen_probe, problems=problems,
    )


def _default_probe(data: pd.DataFrame, charter: RoleCharter, derived: Derived | None = None) -> str | None:
    blocked = set(charter.constraints.forbidden_features) | set(charter.constraints.leakage_features)
    if derived is not None:   # a probe must not be one of the tautologies the screens exist to refuse
        blocked |= set(derived.excluded()) | set(derived.leaks)
    for col in data.columns:
        if col in blocked or gates.is_sensitive_column(col) or not col.isidentifier():
            continue
        if pd.api.types.is_numeric_dtype(data[col]) and data[col].nunique() > 2:
            return f"{col} > {data[col].median()}"
    return None


# Suffixes and prefixes that describe a measure's KIND rather than its subject. Every
# boolean in a warehouse ends `_flag`; every derived ratio ends `_rate`. Matching on them
# would make any two flags look like plausible remaps of each other, which is how a
# category error gets laundered into a signed charter.
_KIND_SUFFIXES = ("flag", "rate", "amount", "count", "score", "pct", "percent", "total",
                  "num", "id", "ind", "indicator", "value")
_KIND_PREFIXES = ("mean", "avg", "total", "num", "is", "has")


def _stem(word: str) -> str:
    # Inflection only — `lapsed`/`lapse`/`lapses` are one subject. Anything cleverer
    # would start to merge subjects, which is exactly what this function must not do.
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _subject(name: str) -> set[str]:
    words = gates.column_words(name)
    while words and words[0] in _KIND_PREFIXES:
        words = words[1:]
    while words and words[-1] in _KIND_SUFFIXES:
        words = words[:-1]
    return {_stem(w) for w in words}


def _plausible_columns(metric: str, columns: list[str], limit: int = 6) -> list[str]:
    """Rank the columns a human might have meant, best first — or return NOTHING.

    Returning an empty list is the important behaviour. A remap is only a sensible
    question when the charter plainly belongs to this data and one field is dressed
    differently — `attrition_flag` meeting an export that calls it `Attrition`. Pairing a
    claims charter with an HR export must not offer to remap one `_flag` onto another.
    """
    subject = _subject(metric)
    scored: list[tuple[float, str]] = []
    for column in columns:
        if not column.isidentifier():      # a metric must survive `Accountability.metric`
            continue
        shared = subject & _subject(column)
        ratio = difflib.SequenceMatcher(None, metric.lower(), column.lower()).ratio()
        if not shared and ratio < 0.72:
            continue
        scored.append((len(shared) + ratio, column))
    scored.sort(reverse=True)
    return [column for _, column in scored[:limit]]


# --------------------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------------------


class Resolution(BaseModel):
    charter: RoleCharter
    remapped: dict[str, str] = Field(default_factory=dict)   # accountability id -> column
    dropped: list[str] = Field(default_factory=list)         # accountability ids opted out
    refused: list[str] = Field(default_factory=list)


def resolve(charter: RoleCharter, report: IntakeReport, answers: dict[str, str]) -> Resolution:
    """Apply answers keyed by accountability id. Refusals are returned, not raised: a
    person answering three questions should not lose two good answers to one bad one.

    Any applied answer returns a DRAFT with its signature cleared; the caller must
    `sign()` it again, by name, before running. That name is the audit trail for the
    remap.
    """
    asks = {c.accountability_id: c for c in report.clarifications}
    updated = charter.model_copy(deep=True)
    out = Resolution(charter=updated)
    keep = list(updated.accountabilities)

    for acc_id, raw in answers.items():
        value = (raw or "").strip()
        if not value:
            continue
        ask = asks.get(acc_id)
        if ask is None:
            out.refused.append(f"{acc_id}: not an open question on this pairing")
            continue
        if value not in ask.candidates:
            out.refused.append(f"{acc_id}: {value!r} is not one of the offered candidates")
            continue
        if value == NOTHING_MEASURES_IT:
            keep = [a for a in keep if a.id != acc_id]
            out.dropped.append(acc_id)
            continue
        next(a for a in keep if a.id == acc_id).metric = value
        out.remapped[acc_id] = value

    if out.remapped or out.dropped:
        updated.accountabilities = keep
        updated.status = "DRAFT"
        updated.ratified_by = None
        updated.ratified_at = None
        updated.content_hash = None
    return out
