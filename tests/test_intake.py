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
from loop.intake import NOTHING_MEASURES_IT, _plausible_columns, pair, resolve

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
