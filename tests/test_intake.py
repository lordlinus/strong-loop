"""The pairing step: what it offers, and — harder — what it refuses to offer.

The value of a clarification is that its candidate list is closed and honest. A list
that contains a wrong answer dressed as a right one is worse than no list at all.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import pytest

from loop.charter import Accountability, Constraints, RoleCharter, load_charter, sign
from loop.intake import NOTHING_MEASURES_IT, _plausible_columns, normalize_columns, pair, resolve

SERVICE = pathlib.Path(__file__).resolve().parent.parent / "src" / "strong-loop"


@pytest.fixture
def data() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    n = 400
    return pd.DataFrame({
        "customer_id": [f"C{i}" for i in range(n)],
        "gender": rng.choice(["m", "f"], n),
        "tenure_years": rng.gamma(2, 3, n),
        "channel": rng.choice(["web", "agent"], n),
        "Attrition": rng.integers(0, 2, n),
        "leakage_flag": rng.integers(0, 2, n),
    })


def charter_for(*metrics: str, **kw) -> RoleCharter:
    return RoleCharter(
        role="r",
        accountabilities=[
            Accountability(id=f"a{i}", statement=f"own {m}", metric=m, direction="increase")
            for i, m in enumerate(metrics)
        ],
        constraints=Constraints(**kw),
    )


# ---- shipped pairings ------------------------------------------------------------------

@pytest.mark.parametrize("charter,csv", [
    ("claims_analyst.yaml", "claims.csv"), ("portfolio_analyst.yaml", "customers.csv"),
])
def test_shipped_pairings_are_clean(charter, csv):
    r = pair(load_charter(SERVICE / "charters" / charter), pd.read_csv(SERVICE / "data" / csv))
    assert r.ok, r.problems
    assert not r.clarifications
    assert r.gate_probe and r.gate_probe.verdict != "REFUSED"
    assert r.screen_probe and r.screen_probe.verdict == "REFUSED"
    assert all(a.status == "ok" for a in r.accountabilities)


def test_cross_domain_pairing_is_a_mismatch_not_a_menu():
    """A claims charter against a customer export must report missing metrics and ask
    NOTHING — none of the customer columns share a subject with the claims measures."""
    r = pair(load_charter(SERVICE / "charters" / "claims_analyst.yaml"),
             pd.read_csv(SERVICE / "data" / "customers.csv"))
    assert not r.ok
    assert [a.status for a in r.accountabilities] == ["missing"] * 3
    assert r.clarifications == []


# ---- screens in the report -------------------------------------------------------------

def test_report_names_screened_columns_with_reasons(data):
    r = pair(charter_for("Attrition", forbidden_features=["channel"]), data)
    reasons = {s.column: s.reason for s in r.screened}
    assert "customer_id" in reasons and "gender" in reasons
    assert reasons["channel"] == "forbidden by charter"
    assert "customer_id" not in r.analysable_columns
    assert r.screen_probe.verdict == "REFUSED" and "sensitive" in r.screen_probe.reason


# ---- clarifications --------------------------------------------------------------------

def test_dressed_differently_gets_a_closed_list_with_opt_out_last(data):
    r = pair(charter_for("attrition_flag"), data)
    assert not r.ok
    (c,) = r.clarifications
    assert c.accountability_id == "a0"
    assert c.candidates[0] == "Attrition"
    assert c.candidates[-1] == NOTHING_MEASURES_IT
    assert "leakage_flag" not in c.candidates


def test_shared_kind_marker_alone_earns_nothing(data):
    """`_flag` is a kind, not a subject. Two flags about different things are not remaps."""
    assert _plausible_columns("churn_flag", list(data.columns)) == []
    assert _plausible_columns("leakage_flag", ["attrition_flag", "is_flagged"]) == []


def test_nothing_plausible_means_no_question(data):
    r = pair(charter_for("settled_amount"), data)
    assert not r.ok
    assert r.clarifications == []


def test_screened_columns_are_never_offered(data):
    # `gender` is protected; even a near-identical metric name must not surface it.
    r = pair(charter_for("gender_x"), data)
    assert all("gender" not in c.candidates for c in r.clarifications)


# ---- resolve ---------------------------------------------------------------------------

def test_resolve_remaps_and_unsigns(data):
    charter = sign(charter_for("attrition_flag"), "alice")
    r = pair(charter, data)
    res = resolve(charter, r, {"a0": "Attrition"})
    assert res.refused == []
    assert res.remapped == {"a0": "Attrition"}
    assert res.charter.accountabilities[0].metric == "Attrition"
    assert res.charter.status == "DRAFT" and res.charter.content_hash is None
    assert charter.status == "SIGNED"                       # input untouched
    assert pair(res.charter, data).ok


def test_resolve_opt_out_drops_the_accountability(data):
    charter = charter_for("Attrition", "attrition_flag")
    r = pair(charter, data)
    res = resolve(charter, r, {"a1": NOTHING_MEASURES_IT})
    assert res.dropped == ["a1"]
    assert [a.id for a in res.charter.accountabilities] == ["a0"]
    assert pair(res.charter, data).ok


def test_resolve_refuses_free_text_and_unasked_questions(data):
    charter = sign(charter_for("Attrition", "attrition_flag"), "alice")
    r = pair(charter, data)
    res = resolve(charter, r, {
        "a1": "tenure_years",        # a real column, but not one that was offered
        "a0": "Attrition",           # not an open question
        "nope": "x",
    })
    assert len(res.refused) == 3
    assert res.remapped == {} and res.dropped == []
    assert res.charter.status == "SIGNED"                   # nothing applied, nothing unsigned
    assert res.charter.content_hash == charter.content_hash


def test_resolve_keeps_good_answers_beside_a_bad_one(data):
    charter = charter_for("attrition_flag", "premium_amount")
    r = pair(charter, data)
    res = resolve(charter, r, {"a0": "Attrition", "a1": "tenure_years"})
    assert res.remapped == {"a0": "Attrition"}
    assert len(res.refused) == 1


# ---- normalize_columns ------------------------------------------------------------------

def test_normalize_leaves_existing_identifiers_untouched():
    """`Attrition`, `attrition_flag`, camelCase headers: already-working pairings must
    see exactly the same columns after normalization as before it."""
    df = pd.DataFrame({"Attrition": [1], "attrition_flag": [0], "JobSatisfaction": [3]})
    out, labels = normalize_columns(df)
    assert list(out.columns) == list(df.columns)
    assert labels == {}


def test_normalize_makes_raw_exports_matchable():
    """Real headers carry spaces, punctuation and leading digits — none of which is a
    valid identifier, so no gate expression or metric match could ever use them as-is."""
    df = pd.DataFrame({
        "Foundry Adopted": [1], "# Pillars Met": [2], "Tools & IQ ACR": [3], "TPID": [4],
    })
    out, labels = normalize_columns(df)
    assert all(c.isidentifier() for c in out.columns)
    assert out.columns[3] == "TPID"                       # already clean, untouched
    assert labels["foundry_adopted"] == "Foundry Adopted"
    assert labels["pillars_met"] == "# Pillars Met"
    assert labels["tools_iq_acr"] == "Tools & IQ ACR"


def test_normalize_dedupes_collisions():
    df = pd.DataFrame({"Foundry Adopted": [1], "foundry_adopted": [2]})
    out, labels = normalize_columns(df)
    assert len(set(out.columns)) == 2
    assert "foundry_adopted" in out.columns and "foundry_adopted_2" in out.columns


def test_normalized_raw_export_yields_candidates_pair_would_otherwise_refuse():
    """The bug this guards: `_plausible_columns` requires `column.isidentifier()`, so a
    raw header with no chance to normalize offers zero candidates no matter how good the
    semantic match is. After normalization, the same data yields a real menu."""
    charter = charter_for("foundry_adopted_flag")
    raw = pd.DataFrame({"Foundry Adopted": [1, 0, 1], "TPID": [1, 2, 3]})
    data, labels = normalize_columns(raw)
    r = pair(charter, data, labels)
    assert not r.ok
    (c,) = r.clarifications
    assert "foundry_adopted" in c.candidates


# ------------------------------------------------------------------ derived screens
def test_a_constant_metric_withholds_its_question_and_the_run_proceeds(data):
    """Seen live: `product_adopted_flag` was all zeros and intake said [ok]."""
    charter = charter_for("Attrition", "flat")
    r = pair(charter, data.assign(flat=0))
    flat = next(a for a in r.accountabilities if a.metric == "flat")
    assert flat.status == "unmeasurable" and "constant" in flat.measure["reason"]
    assert r.ok, "one dead metric must not refuse the run"
    assert len(r.questions) == 1 and len(r.withheld) == 1 and "flat" in r.withheld[0]


def test_every_metric_dead_is_a_problem(data):
    r = pair(charter_for("flat"), data.assign(flat=0))
    assert not r.ok
    assert any("no accountability can be measured" in p for p in r.problems)


def test_columns_that_define_a_metric_are_reported_and_the_probe_avoids_them(data):
    d = data.assign(attrition_x10=data["Attrition"] * 10, dup_channel=data["channel"])
    r = pair(charter_for("Attrition"), d)
    reasons = {s.column: s.reason for s in r.derived}
    assert "attrition_x10" in reasons and "Attrition" in reasons["attrition_x10"]
    assert "dup_channel" in {s.column for s in r.screened}, "an alias is never offered"
    assert r.gate_probe is not None and "attrition_x10" not in r.gate_probe.where
    assert r.ok


# ---- normalize_values: text that is really a number ------------------------------------

def test_yes_no_outcome_is_read_as_0_1_and_reported():
    """Seen on a churn export: `Churn` in Yes/No was coerced to all-missing then all-zero,
    reported unmeasurable, and the probe claimed every row had `Churn == 0`."""
    raw = pd.DataFrame({"Churn": ["Yes", "No", " no", "YES", "No", "Yes"] * 60,
                        "tenure": list(range(360))})
    data, _ = normalize_columns(raw)
    assert data["Churn"].tolist()[:4] == [1, 0, 0, 1]
    assert "YES/Yes read as 1" in data.attrs["recoded"]["Churn"]
    r = pair(charter_for("Churn"), data)
    assert r.accountabilities[0].status == "ok" and "Churn" in r.recoded
    assert r.gate_probe is not None and r.gate_probe.verdict != "REFUSED"
    assert raw["Churn"].iloc[0] == "Yes" and "recoded" not in raw.attrs, "input untouched"


def test_ambiguous_text_is_left_as_text():
    """No safe 0/1 reading: two non-yes/no labels, a single answer, leading-zero codes,
    and anything with a non-number in it."""
    raw = pd.DataFrame({
        "tier": ["Gold", "Silver"] * 3,
        "only_no": ["No"] * 6,
        "postcode": ["02134", "10001"] * 3,
        "amount": ["$5", "6"] * 3,
    })
    data, _ = normalize_columns(raw)
    assert data.attrs["recoded"] == {}
    for col in raw.columns:
        assert data[col].tolist() == raw[col].tolist()


def test_numbers_stored_as_text_become_numbers_and_blanks_missing():
    raw = pd.DataFrame({"TotalCharges": ["29.85", " ", "1889.5", "108.15"]})
    data, _ = normalize_columns(raw)
    assert pd.api.types.is_float_dtype(data["TotalCharges"])
    assert data["TotalCharges"].isna().tolist() == [False, True, False, False]
    assert "1 blank" in data.attrs["recoded"]["TotalCharges"]


def test_a_text_metric_gets_no_probe_rather_than_a_coerced_verdict(data):
    r = pair(charter_for("tier"), data.assign(tier=["Gold", "Silver"] * 200))
    assert r.gate_probe is None
    assert any("not numeric or 0/1" in p for p in r.problems)


# ---- signature status --------------------------------------------------------------------

def test_an_unsigned_charter_is_a_draft_even_if_its_yaml_says_signed(tmp_path):
    assert charter_for("x").status == "DRAFT"
    assert RoleCharter.model_validate({**charter_for("x").model_dump(), "status": "SIGNED"}).status == "DRAFT"
    assert sign(charter_for("x"), "alice").status == "SIGNED"
    assert load_charter(SERVICE / "charters" / "claims_analyst.yaml").status == "DRAFT"
