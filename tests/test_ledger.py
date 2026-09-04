"""The ledger is the loop's only memory. These pin down what it must and must not do."""

from __future__ import annotations

import json

from loop.ledger import Ledger, fingerprint
from loop.types import Evidence, Hypothesis, Question, Verdict


def _hyp(i: int, where: str | None = None) -> Hypothesis:
    return Hypothesis(question_id="q1", statement=f"claim {i}", kind="proportion_lift",
                      spec={"where": where or f"x > {i}"})


def test_append_is_durable_and_typed(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.append(Question(accountability_id="a1", text="why?"))
    h = ledger.append(_hyp(1))
    ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift", verdict=Verdict.REJECTED))
    reopened = Ledger(tmp_path)  # a fresh process sees the same record
    assert len(reopened.all("question")) == 1
    assert len(reopened.all("hypothesis")) == 1
    assert reopened.all("evidence")[0].hypothesis_id == h.id


def test_fingerprint_ignores_wording_and_key_order():
    a = Hypothesis(question_id="q", statement="long tenure converts", kind="proportion_lift",
                   spec={"where": "tenure > 8", "target": "converted"})
    b = Hypothesis(question_id="q", statement="people who stayed longer buy more",
                   kind="proportion_lift", spec={"target": "converted", "where": "tenure > 8"})
    assert fingerprint(a) == fingerprint(b)
    ledger_specs = {fingerprint(a)}
    assert fingerprint(b) in ledger_specs, "a rewording must read as already tried"


def test_summary_stays_small_as_the_run_grows(tmp_path):
    """The summary is loaded into every fresh context; its size is a tax on thinking."""
    ledger = Ledger(tmp_path)
    for i in range(60):
        h = ledger.append(_hyp(i, where=f"(tenure > {i}) & (region == 'north') & (plan == 'gold')"))
        ledger.append(Evidence(hypothesis_id=h.id, gate="proportion_lift",
                               verdict=Verdict.REJECTED, p_value=0.5, effect_size=1.0, sample_size=300))
    text = json.dumps(ledger.summary(), default=str)
    assert len(text) / 4 < 1500, f"summary is ~{len(text) // 4} tokens"
    summary = ledger.summary()
    assert summary["hypotheses_tested"] == 60
    assert len(summary["recent_tests"]) == 8
