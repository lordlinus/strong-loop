"""Domain context enters through the charter — a glossary and leads — and reaches the
model and the intake mapping verbatim. No term is interpreted by code."""

from __future__ import annotations

import asyncio

import pandas as pd

from loop.charter import Accountability, Constraints, RoleCharter
from loop.ledger import Ledger
from loop.questions import questions_from
from loop.tools import Toolbelt


def _charter() -> RoleCharter:
    return RoleCharter(
        role="r",
        glossary={"agent": "a workload on the hosted agent runtime",
                  "LLM API": "direct model calls on the customer's own runtime"},
        accountabilities=[Accountability(
            id="a1", statement="grow agent tokens", metric="tokens", direction="increase",
            leads=["heavy LLM API users with zero agent tokens are the addressable pool"],
        )],
        constraints=Constraints(),
    )


def test_glossary_and_leads_reach_the_model(tmp_path):
    data = pd.DataFrame({"tokens": [0, 1, 2, 3] * 10, "x": list(range(40))})
    belt = Toolbelt(_charter(), data, Ledger(tmp_path))
    charter = belt.get_charter()
    assert charter["glossary"]["agent"].startswith("a workload")
    assert charter["accountabilities"][0]["leads"][0].startswith("heavy LLM API users")
    (q,) = questions_from(_charter())
    assert "Leads from the charter" in q.why_it_matters and "addressable pool" in q.why_it_matters


def test_a_lead_free_charter_reads_as_before():
    plain = _charter().model_copy(update={"glossary": {}})
    plain.accountabilities[0].leads = []
    (q,) = questions_from(plain)
    assert "Leads" not in q.why_it_matters


def test_glossary_travels_with_the_typesafe_mapping_request(monkeypatch):
    from loop.intake import pair
    from loop.suggest import suggest_mappings
    seen = {}

    async def fake(columns, accountabilities, model, glossary=None):
        seen["glossary"] = glossary
        return {}

    monkeypatch.setattr("loop.suggest._ask", fake)
    data = pd.DataFrame({"pillars_met": list(range(20)), "channel": ["a", "b"] * 10})
    charter = _charter()
    asyncio.run(suggest_mappings(charter, data, pair(charter, data)))
    assert seen["glossary"] == charter.glossary
