"""
stats.py — dependency-free statistics used by the gates.

Deliberately implemented with the standard library and numpy only. The gates are the
platform's trust boundary; keeping their maths readable and dependency-light means the
whole chain of reasoning can be audited without pulling in a stack of numerical
libraries.
"""

from __future__ import annotations

import math

import numpy as np


def norm_sf(z: float) -> float:
    """Upper tail of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_p_1dof(chi2: float) -> float:
    if chi2 <= 0:
        return 1.0
    return 2.0 * norm_sf(math.sqrt(chi2))


def fisher_exact_2x2(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for a 2x2 table. Used when expected cells are thin."""
    from math import lgamma

    n = a + b + c + d
    if n == 0:
        return 1.0

    def lhyper(a_: int, b_: int, c_: int, d_: int) -> float:
        r1, r2, c1, c2 = a_ + b_, c_ + d_, a_ + c_, b_ + d_
        return (
            lgamma(r1 + 1) + lgamma(r2 + 1) + lgamma(c1 + 1) + lgamma(c2 + 1)
            - lgamma(n + 1) - lgamma(a_ + 1) - lgamma(b_ + 1)
            - lgamma(c_ + 1) - lgamma(d_ + 1)
        )

    p_obs = lhyper(a, b, c, d)
    r1, c1, r2 = a + b, a + c, c + d
    lo, hi = max(0, c1 - r2), min(c1, r1)
    total = 0.0
    for a2 in range(lo, hi + 1):
        b2, c2, d2 = r1 - a2, c1 - a2, n - r1 - (c1 - a2)
        if b2 < 0 or c2 < 0 or d2 < 0:
            continue
        lp = lhyper(a2, b2, c2, d2)
        if lp <= p_obs + 1e-9:
            total += math.exp(lp)
    return min(1.0, total)


def two_proportion_test(a: int, b: int, c: int, d: int) -> tuple[float, str]:
    """Chi-square with a Fisher fallback for thin cells. Returns (p, test_name)."""
    n = a + b + c + d
    if n == 0:
        return 1.0, "none"
    obs = np.array([[a, b], [c, d]], dtype=float)
    row = obs.sum(axis=1, keepdims=True)
    col = obs.sum(axis=0, keepdims=True)
    exp = row * col / n
    if exp.min() < 5:
        return fisher_exact_2x2(a, b, c, d), "fisher"
    chi2 = float(((obs - exp) ** 2 / exp).sum())
    return chi2_p_1dof(chi2), "chi2"


def welch_t_test(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Welch's t-test. Returns (p_value, cohens_d). Normal approximation for the tail."""
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2:
        return 1.0, 0.0
    mx, my = float(np.mean(x)), float(np.mean(y))
    vx, vy = float(np.var(x, ddof=1)), float(np.var(y, ddof=1))
    se = math.sqrt(vx / nx + vy / ny)
    if se == 0:
        return 1.0, 0.0
    t = (mx - my) / se
    pooled = math.sqrt(((nx - 1) * vx + (ny - 1) * vy) / max(1, nx + ny - 2))
    d = (mx - my) / pooled if pooled > 0 else 0.0
    return 2.0 * norm_sf(abs(t)), d


def mann_whitney_p(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided Mann-Whitney U via normal approximation, with tie correction."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return 1.0
    allv = np.concatenate([a, b])
    order = allv.argsort()
    ranks = np.empty(len(allv), dtype=float)
    ranks[order] = np.arange(1, len(allv) + 1)
    _, inv, counts = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    ranks = (sums / counts)[inv]
    u1 = ranks[:n1].sum() - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    if sigma == 0:
        return 1.0
    return 2.0 * norm_sf(abs((u1 - mu) / sigma))


def benjamini_hochberg(pvals: dict[str, float | None]) -> dict[str, float | None]:
    """FDR control across every hypothesis tested in a run.

    An autonomous loop tests hundreds of hypotheses, so uncorrected p-values are close to
    meaningless — at alpha=0.05, 200 null hypotheses yield ~10 "discoveries" by chance.
    This correction is what stops a tireless agent from confidently reporting noise.
    """
    items = [(k, v) for k, v in pvals.items() if v is not None]
    out: dict[str, float | None] = {k: None for k in pvals}
    m = len(items)
    if m == 0:
        return out
    ordered = sorted(items, key=lambda kv: kv[1])
    prev = 1.0
    for i in range(m - 1, -1, -1):
        key, p = ordered[i]
        prev = min(prev, p * m / (i + 1))
        out[key] = min(1.0, prev)
    return out


def mde_lift(n: int, base_rate: float, power: float = 0.8, alpha: float = 0.05) -> float:
    """Minimum detectable lift at this sample size.

    Reported alongside every result so a REJECTED verdict can be read correctly: it tells
    the agent whether the effect was absent or merely too small to see — the difference
    between "stop exploring here" and "get more data".
    """
    if n <= 0 or base_rate <= 0 or base_rate >= 1:
        return float("nan")
    za, zb = 1.959963985, 0.8416212336
    delta = (za + zb) * math.sqrt(base_rate * (1 - base_rate) / n)
    return (base_rate + delta) / base_rate


def stratified_mean_difference(
    values: np.ndarray, mask: np.ndarray, strata: np.ndarray
) -> tuple[float, float, int]:
    """Inside-vs-outside mean difference pooled across strata: the continuous-outcome
    counterpart of Mantel-Haenszel.

    Within each stratum, Cohen's d from a Welch comparison; strata pooled by inverse
    variance (var(d) ~ (n1+n2)/(n1 n2) + d^2 / 2(n1+n2)). Returns (p, pooled_d, strata
    used). Strata with fewer than two rows on either side contribute nothing.
    """
    num = den = 0.0
    used = 0
    # A missing control value is not a stratum. Coerce to plain strings first: pandas'
    # string dtype keeps NaN as a float, and `np.unique` cannot order float against str.
    labels = np.array([None if (isinstance(v, float) and math.isnan(v)) or v is None else str(v)
                       for v in strata], dtype=object)
    for level in {v for v in labels if v is not None}:
        sel = labels == level
        x = values[sel & mask]
        y = values[sel & ~mask]
        x, y = x[~np.isnan(x)], y[~np.isnan(y)]
        if len(x) < 2 or len(y) < 2:
            continue
        _, d = welch_t_test(x, y)
        n1, n2 = len(x), len(y)
        var = (n1 + n2) / (n1 * n2) + d * d / (2.0 * (n1 + n2))
        if var <= 0:
            continue
        num += d / var
        den += 1.0 / var
        used += 1
    if used == 0 or den == 0:
        return 1.0, 0.0, used
    pooled = num / den
    z = pooled / math.sqrt(1.0 / den)
    return 2.0 * norm_sf(abs(z)), pooled, used

