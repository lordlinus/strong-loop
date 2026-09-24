"""derived.py: the data, not a charter list, decides what may not be tested.

Every rule is exercised with a planted case AND a near-miss that must stay silent, because
a screen that over-blocks removes the very drivers the loop exists to find.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from loop.charter import Accountability, Constraints, EvidenceStandard, RoleCharter
from loop.derived import Derived, derive


def charter_for(*metrics: str, min_sample_size: int = 30) -> RoleCharter:
    return RoleCharter(
        role="r",
        accountabilities=[
            Accountability(id=f"a{i}", statement=f"own {m}", metric=m, direction="increase")
            for i, m in enumerate(metrics)
        ],
        evidence_standards=EvidenceStandard(min_sample_size=min_sample_size),
        constraints=Constraints(),
    )


@pytest.fixture
def frame() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    n = 600
    pillars = rng.integers(0, 4, n)                      # 0..3, the raw count
    tokens = np.where(rng.random(n) < 0.15, rng.integers(1, 10_000, n), 0)
    total = tokens + rng.integers(100, 1_000_000, n)   # wide denominators: share is not monotone in tokens
    return pd.DataFrame({
        "record_id": [f"R{i}" for i in range(n)],
        "region": rng.choice(["n", "s", "e"], n),
        "region_code": None,                             # filled below: an alias of region
        "support": rng.choice(["yes", "no"], n),
        "pillars": pillars,
        "coverage": pillars / 4,                          # rescaling of pillars
        "acquired": (pillars >= 2).astype(int),          # threshold of coverage
        "tokens": tokens,                                 # numerator, unique when non-zero
        "share": tokens / total,                          # the metric tokens feeds
        "constant": 1,
        "flag": rng.integers(0, 2, n),
        "rare": (rng.random(n) < 0.01).astype(int),
        "same_as_flag": None,
    }).assign(
        region_code=lambda d: d["region"].map({"n": 1, "s": 2, "e": 3}),
        same_as_flag=lambda d: d["flag"],
    )


class TestMetrics:
    def test_a_constant_metric_is_unmeasurable_not_a_leak_magnet(self, frame):
        d = derive(frame, charter_for("constant"))
        assert d.unmeasurable["constant"].startswith("constant")
        assert d.leaks == {}, "everything determines a constant; none of it is a leak"

    def test_a_nearly_constant_binary_metric_is_warned_not_withheld(self, frame):
        d = derive(frame, charter_for("rare"))
        assert "rare" not in d.unmeasurable
        assert any("nearly constant" in w and "'rare'" in w for w in d.warnings)

    def test_a_healthy_binary_metric_has_no_warning(self, frame):
        assert derive(frame, charter_for("flag")).warnings == []


class TestLeaks:
    def test_rescaling_is_a_leak_of_that_metric_only(self, frame):
        d = derive(frame, charter_for("coverage", "share"))
        assert "coverage" in d.leaks["pillars"]
        assert "share" not in d.leaks.get("pillars", {}), "pillars may still drive share"

    def test_threshold_of_the_metric_is_a_leak(self, frame):
        d = derive(frame, charter_for("coverage"))
        assert "threshold or relabelling" in d.leaks["acquired"]["coverage"]

    def test_a_numerator_with_unique_values_is_not_a_functional_leak(self, frame):
        d = derive(frame, charter_for("share"))
        assert "share" not in d.leaks.get("tokens", {}), "one-row groups make the dependency vacuous"

    def test_perfect_separation_of_a_binary_metric(self, frame):
        data = frame.assign(is_error=(frame["region"] == "e").astype(int))
        d = derive(data, charter_for("is_error"))
        assert "is_error" in d.leaks["region"]

    def test_an_honest_driver_is_untouched(self, frame):
        d = derive(frame, charter_for("flag", "share", "coverage"))
        for column in ("region", "support"):
            assert column not in d.leaks and column not in d.suspected

    def test_leak_reason_is_scoped_to_the_outcome(self, frame):
        d = derive(frame, charter_for("coverage", "share"))
        assert d.leak_reason("pillars", "coverage")
        assert d.leak_reason("pillars", "share") is None
        assert d.leak_reason("pillars", None) is None


class TestColumns:
    def test_constants_and_aliases_are_excluded(self, frame):
        d = derive(frame, charter_for("share"))
        assert "constant" in d.constants
        assert d.aliases.get("region_code") == "region"
        assert d.aliases.get("same_as_flag") == "flag"
        assert "flag" not in d.aliases, "the first column is the one kept"
        # `coverage` is pillars/4 and, when it is not a metric, simply a duplicate column.
        assert d.aliases.get("coverage") == "pillars"
        assert set(d.excluded()) == {"constant", "region_code", "same_as_flag", "coverage"}

    def test_a_metric_is_never_reported_as_an_alias(self, frame):
        d = derive(frame, charter_for("flag"))
        assert "flag" not in d.aliases
        assert "same_as_flag" in d.leaks, "an identical column IS a leak of that metric"

    def test_empty_frame_is_quiet(self):
        d = derive(pd.DataFrame({"x": []}), charter_for("x"))
        assert d == Derived()


class TestMerge:
    def test_code_tier_wins_and_semantic_fills_gaps(self):
        code = Derived(leaks={"a": {"m": "code says copy"}}, constants={"k": "constant"})
        semantic = Derived(leaks={"a": {"m": "model says copy"}, "b": {"m": "model: component"}},
                           suspected={"a": {"m": "should be dropped, a leaks m already"},
                                      "c": {"m": "model: maybe"}})
        merged = code.merge(semantic)
        assert merged.leaks == {"a": {"m": "code says copy"}, "b": {"m": "model: component"}}
        assert merged.suspected == {"c": {"m": "model: maybe"}}
        assert merged.constants == {"k": "constant"}
        assert [c for c, _ in merged.screened()] == ["k", "a", "b"]
