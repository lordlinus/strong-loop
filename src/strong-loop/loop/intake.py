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
from pydantic import BaseModel, Field, model_validator

from . import gates
from .brief import role_brief
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
    """A question whose answer has exactly one legitimate place to land, chosen from a
    closed list. `id` is what an answer is keyed by; for the two blocking kinds it is the
    accountability id, so `{acc_id: column}` answers keep working.

    Kinds, all generated by code from charter + schema (PLAN.md §6.7):
      blocking — the run is refused until answered
        metric     which column measures a metric the data does not carry by that name
        encoding   which value of a text metric counts as the outcome (read as 1)
      advisory — a default applies when unanswered, and is recorded
        leak       is this column computed from, or recorded after, the metric?
        attribute  may this role use a person's attribute the screens do not block?
        weak       keep or withhold a metric no subgroup can clear the gate on
        term       which column, if any, a glossary term means in this data
    """

    id: str = ""
    kind: str = "metric"
    accountability_id: str = ""
    metric: str = ""
    subject: str = ""       # the column or glossary term an advisory is about
    question: str
    why: str
    candidates: list[str]   # ranked; the opt-out, or the default, is always offered
    default: str | None = None
    answer: str | None = None   # what was chosen, once the pairing is accepted

    @model_validator(mode="after")
    def _id_defaults_to_accountability(self) -> "Clarification":
        if not self.id:
            self.id = self.accountability_id
        return self


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
    clarifications: list[Clarification]            # blocking: answered before any run
    advisories: list[Clarification] = Field(default_factory=list)   # defaults apply if unanswered
    notes: list[str] = Field(default_factory=list)  # what the data's shape means for every finding
    brief: dict[str, Any] = Field(default_factory=dict)   # the persona, `loop.brief.role_brief`
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
         derived: Derived | None = None, context: dict[str, Any] | None = None) -> IntakeReport:
    """`context` is what an accepted pairing added (`settle_advice`): glossary bindings,
    and the advisory answers, which are shown against their questions."""
    if derived is None:
        derived = derive(data, charter)   # the code tier; `settle()` adds the semantic one
    context = context or {}
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
        if present and acc.metric in derived.withheld:
            statuses.append(AccountabilityStatus(id=acc.id, statement=acc.statement,
                                                 metric=acc.metric, status="withheld",
                                                 measure=measure))
            continue
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
            # A text column is in the data but no gate can test it. When it holds a handful
            # of values, which one is "the outcome" is a question a person can answer from
            # a closed list; anything wider is a source-side fix.
            values = _outcome_values(data[acc.metric])
            shown = ", ".join(values[:4]) or ", ".join(map(str, data[acc.metric].dropna().unique()[:4]))
            if values:
                problems.append(f"accountability {acc.id!r} metric {acc.metric!r} holds text "
                                f"({shown}); choose which value counts as the outcome")
                clarifications.append(Clarification(
                    kind="encoding", accountability_id=acc.id, metric=acc.metric,
                    question=(f"{acc.metric!r} holds text, not numbers. Which value counts as "
                              f"the outcome? It is read as 1, every other value as 0."),
                    why=(f"'{acc.statement}' is about this column moving. A gate compares rates, "
                         f"so one value has to be the event. Choosing {NOTHING_MEASURES_IT!r} "
                         f"leaves it out of this run."),
                    candidates=values + [NOTHING_MEASURES_IT],
                ))
            else:
                problems.append(f"accountability {acc.id!r} metric {acc.metric!r} is not numeric or 0/1 "
                                f"(values like: {shown}); derive a 0/1 column in the data and target that")
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

    advice = dict(context.get("advice") or {})
    advisories = [a.model_copy(update={"answer": advice[a.id]}) if a.id in advice else a
                  for a in advisories_for(charter, data, derived)]
    notes = data_notes(data, analysable)
    brief = role_brief(charter, data, derived, {"bindings": context.get("bindings"), "notes": notes},
                       measures=prof["accountability_measures"])

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
        advisories=advisories, notes=notes, brief=brief,
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


_MAX_OUTCOME_VALUES = 12


def _outcome_values(series: pd.Series) -> list[str]:
    """The values a person could pick as "the outcome" of a text metric, most frequent
    first — or none, when there are too many for a pick to be a real decision."""
    if pd.api.types.is_numeric_dtype(series):
        return []
    counts = series.dropna().astype(str).str.strip().replace("", pd.NA).dropna().value_counts()
    return [str(v) for v in counts.index] if 2 <= len(counts) <= _MAX_OUTCOME_VALUES else []


def encode(data: pd.DataFrame, encoded: dict[str, str]) -> pd.DataFrame:
    """Apply `{metric: value}` picks: the chosen value reads as 1, every other value as 0,
    blanks stay missing. Returns a new frame; `attrs` carry what was read, like `recoded`."""
    if not encoded:
        return data
    out = data.copy()
    recoded = dict(out.attrs.get("recoded") or {})
    for metric, value in encoded.items():
        if metric not in out.columns:
            continue
        text = out[metric].astype("string").str.strip()
        out[metric] = (text == value).astype("float64").where(text.notna() & (text != ""))
        if out[metric].notna().all():
            out[metric] = out[metric].astype("int64")
        recoded[metric] = f"{value!r} read as 1, every other value as 0 (chosen at intake)"
    out.attrs["recoded"] = recoded
    return out


# Words that name a person's attribute which the screens deliberately do not block — a role
# may have a legitimate reason to use it, or none. A person decides, per role, at intake.
_ATTRIBUTE_WORDS = {"age", "senior", "dependents", "dependent", "veteran", "birth", "born"}
_TIME_WORDS = {"date", "datetime", "timestamp", "month", "week", "quarter", "period", "year"}
_WINDOW_WORDS = {"last", "per", "this", "next", "since", "prior", "previous"}

LEAK_YES = "yes — it is computed from, or recorded after, the metric; never test it for this metric"
LEAK_NO = "no — it is an independent attribute; test it normally"
LEAK_UNSURE = "not sure — test it, and flag any evidence that uses it"
ATTR_EXCLUDE = "no — never use it in a rule for this role"
ATTR_ALLOW = "yes — this role may use it"
WEAK_KEEP = "keep it — the question stays open"
WEAK_WITHHOLD = "withhold it — leave it out of this run"
TERM_NONE = "(not in this data)"


def _names_a_period(column: str) -> bool:
    """`order_date`, `signup_month`, `period` name a time axis; `contacts_last_year` is a
    count over a window and does not."""
    words = gates.column_words(column)
    return bool(words) and words[-1] in _TIME_WORDS and not _WINDOW_WORDS & set(words)


def _looks_like_dates(series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return False
    sample = series.dropna().astype(str).head(50)
    if sample.empty or not sample.str.contains(r"\d").all():
        return False
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(sample, errors="coerce")
    return bool(parsed.notna().mean() >= 0.9)


def data_notes(data: pd.DataFrame, analysable: list[str]) -> list[str]:
    """What the shape of the data means for every finding drawn from it."""
    for col in analysable:
        if _looks_like_dates(data[col]) or _names_a_period(col):
            return []
    return ["No column holds a date or period: this is a snapshot, so every finding is an "
            "association at one point in time, not a change over time."]


def advisories_for(charter: RoleCharter, data: pd.DataFrame, derived: Derived) -> list[Clarification]:
    """Questions a person can settle better than code, each with a default that keeps the
    run safe if nobody answers. Generated from charter + schema; no model."""
    out: list[Clarification] = []
    metrics = {a.metric for a in charter.accountabilities}
    by_metric = {a.metric: a for a in charter.accountabilities}

    for col, per_metric in sorted(derived.suspected.items()):
        for metric, reason in sorted(per_metric.items()):
            out.append(Clarification(
                id=f"leak:{col}:{metric}", kind="leak", subject=col, metric=metric,
                accountability_id=by_metric[metric].id if metric in by_metric else "",
                question=f"Is {col!r} computed from, or recorded after, {metric!r}?",
                why=(f"The data suggests it but cannot prove it ({reason}). If yes, a rule on "
                     f"{col!r} would only restate the metric; it stays usable for other metrics."),
                candidates=[LEAK_UNSURE, LEAK_YES, LEAK_NO], default=LEAK_UNSURE,
            ))

    forbidden = set(charter.constraints.forbidden_features)
    excluded = derived.excluded()
    for col in data.columns:
        if (col in metrics or col in forbidden or col in excluded or gates.is_sensitive_column(col)
                or not _ATTRIBUTE_WORDS & set(gates.column_words(col))):
            continue
        out.append(Clarification(
            id=f"attribute:{col}", kind="attribute", subject=col,
            question=f"May this role use {col!r} — a person's attribute — to pick out a group?",
            why=("The screens do not block it because some roles need it, but acting on it can "
                 "be unfair or unlawful. Default is no: it is left out of every rule this run."),
            candidates=[ATTR_EXCLUDE, ATTR_ALLOW], default=ATTR_EXCLUDE,
        ))

    for metric, reason in sorted(derived.weak.items()):
        if metric in derived.withheld or metric in derived.unmeasurable or metric not in by_metric:
            continue
        out.append(Clarification(
            id=f"weak:{by_metric[metric].id}", kind="weak", subject=metric, metric=metric,
            accountability_id=by_metric[metric].id,
            question=f"{metric!r} barely varies in this data. Keep its question, or withhold it?",
            why=(f"{reason[0].upper()}{reason[1:]}; tests on it will mostly come back inconclusive. "
                 f"Withholding it spends the run on questions this data can answer."),
            candidates=[WEAK_KEEP, WEAK_WITHHOLD], default=WEAK_KEEP,
        ))

    analysable = [c for c in data.columns if c not in excluded and c not in forbidden
                  and not gates.is_sensitive_column(c)]
    for term, meaning in sorted(charter.glossary.items()):
        if normalize_column_name(term) in {normalize_column_name(c) for c in data.columns}:
            continue   # the data already calls it by that name
        candidates = _plausible_columns(term, analysable)
        if not candidates:
            continue   # a menu of wrong answers is worse than none; the glossary still reaches the model
        out.append(Clarification(
            id=f"term:{term}", kind="term", subject=term,
            question=f"Which column, if any, is {term!r} in this data?",
            why=f"The charter defines it as: {' '.join(meaning.split())} The answer goes to the agent as a fact about this data.",
            candidates=[TERM_NONE] + candidates, default=TERM_NONE,
        ))
    return out


def apply_advice(derived: Derived, advisories: list[Clarification], chosen: dict[str, str]) -> Derived:
    """Land each advisory answer where it belongs. Returns a new `Derived`."""
    out = derived.merge(Derived())
    for a in advisories:
        value = chosen.get(a.id, a.default)
        if a.kind == "leak":
            reason = (out.suspected.get(a.subject) or {}).pop(a.metric, None)
            if not out.suspected.get(a.subject):
                out.suspected.pop(a.subject, None)
            if value == LEAK_UNSURE:
                if reason is not None:
                    out.suspected.setdefault(a.subject, {})[a.metric] = reason
            elif value == LEAK_YES:
                out.leaks.setdefault(a.subject, {})[a.metric] = f"confirmed at intake: {reason or 'a person said so'}"
        elif a.kind == "attribute" and value == ATTR_EXCLUDE:
            out.protected[a.subject] = "a person's attribute; this role may not use it (decided at intake)"
        elif a.kind == "weak" and value == WEAK_WITHHOLD:
            out.withheld[a.metric] = f"withheld at intake: {out.weak.get(a.metric, 'barely varies')}"
    return out


def settle_advice(charter: RoleCharter, data: pd.DataFrame, derived: Derived,
                  answers: dict[str, str] | None = None) -> tuple[Derived, dict[str, Any]]:
    """Every advisory on this pairing, answered or defaulted, applied. Returns the screens
    to run with and the context a run carries: `advice` (id -> choice), glossary
    `bindings`, and the data `notes`. The same path for the hosted intake, the CLI and a
    session that brought nothing, so a default means the same thing everywhere."""
    asks = advisories_for(charter, data, derived)
    chosen = {}
    for a in asks:
        given = (answers or {}).get(a.id)
        chosen[a.id] = given if given in a.candidates else a.default
    bindings = {a.subject: chosen[a.id] for a in asks if a.kind == "term" and chosen[a.id] != TERM_NONE}
    applied = apply_advice(derived, asks, chosen)
    notes = data_notes(data, [c for c in data.columns if c not in applied.excluded()])
    return applied, {"advice": chosen, "bindings": bindings, "notes": notes}


# --------------------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------------------


class Resolution(BaseModel):
    charter: RoleCharter
    remapped: dict[str, str] = Field(default_factory=dict)   # accountability id -> column
    dropped: list[str] = Field(default_factory=list)         # accountability ids opted out
    encoded: dict[str, str] = Field(default_factory=dict)    # metric -> the value read as 1
    advice: dict[str, str] = Field(default_factory=dict)     # advisory id -> choice, as given
    refused: list[str] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Did an answer change what the role is answerable for? Then someone signs it."""
        return bool(self.remapped or self.dropped or self.encoded)


def resolve(charter: RoleCharter, report: IntakeReport, answers: dict[str, str]) -> Resolution:
    """Apply answers keyed by question id (the accountability id, for the blocking kinds).
    Refusals are returned, not raised: a person answering three questions should not lose
    two good answers to one bad one.

    A remap, opt-out or encoding returns a DRAFT with its signature cleared; the caller
    must `sign()` it again, by name, before running. That name is the audit trail. Advisory
    answers are collected in `advice` for `settle_advice`; they narrow what may be tested
    and never touch the charter.
    """
    asks = {c.id: c for c in report.clarifications}
    advisories = {c.id: c for c in report.advisories}
    updated = charter.model_copy(deep=True)
    out = Resolution(charter=updated)
    keep = list(updated.accountabilities)

    for qid, raw in answers.items():
        value = (raw or "").strip()
        if not value:
            continue
        ask = asks.get(qid) or advisories.get(qid)
        if ask is None:
            out.refused.append(f"{qid}: not an open question on this pairing")
            continue
        if value not in ask.candidates:
            out.refused.append(f"{qid}: {value!r} is not one of the offered candidates")
            continue
        if qid in advisories:
            out.advice[qid] = value
            continue
        acc_id = ask.accountability_id
        if value == NOTHING_MEASURES_IT:
            keep = [a for a in keep if a.id != acc_id]
            out.dropped.append(acc_id)
            continue
        if ask.kind == "encoding":
            out.encoded[ask.metric] = value
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
