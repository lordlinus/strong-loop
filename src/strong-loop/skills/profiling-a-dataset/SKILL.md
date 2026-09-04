---
name: profiling-a-dataset
description: Use once at the start of a run, before forming any hypothesis. The data-engineering checks that decide whether a column can carry a claim at all — missingness, cardinality, coercion, units, survivorship and leakage.
---

# Profiling a dataset

A column that exists is not a column you can reason with. Every gate verdict inherits
whatever was wrong with its inputs, and the gate cannot see the defect — it only sees
numbers. This is the pass that catches it first.

## The six checks

**1. Missingness, and whether it is random.**
`get_data_profile` reports each column's null share. What matters is not how many are missing but
whether missingness correlates with the outcome. A field only populated for customers who
completed onboarding encodes "completed onboarding" into every claim you make with it.
State the suspected mechanism, then avoid the column or test the subgroup that has it.

**2. Cardinality against row count.**
A categorical with cardinality approaching the row count is an identifier wearing a
category's clothes; it will produce beautiful, meaningless subgroups. A categorical with
cardinality 1 carries no information at all. Both are dead ends — find them now rather
than after a refusal.

**3. Type coercion that already happened.**
A numeric column read as text because 0.3% of its values say `"N/A"` will sort
lexicographically and silently break any threshold rule. Check that the dtype is what the
meaning implies, not what the loader guessed.

**4. Units and scale.**
`tenure` in months and `tenure` in days both look like small integers. `amount` may be
minor units. Nothing in the data announces this. If the charter's statement does not say, state
that the unit is unknown rather than assuming one.

**5. Survivorship.**
Ask what had to be true for a row to be in this file. If churned customers are purged, a
churn analysis over it is measuring the survivors' behaviour and will conclude that
nothing predicts churn. This is the defect that most reliably produces a confident wrong
answer, because the data looks complete.

**6. Leakage — the outcome hiding in a predictor.**
A column recorded *after* the outcome, or derived *from* it, will predict it perfectly and
tell you nothing you can act on. `cancellation_reason` predicts cancellation. The charter
lists known leakage in `constraints.leakage_features` and the screens enforce it, but the
charter's author could only list what they knew about. If a column is suspiciously
predictive, suspect leakage before celebrating.

## What to do with the result

Profiling produces **no evidence**. It produces a shortlist of columns worth testing and a
list of columns that cannot carry a claim, plus the reason for each. Carry that into your
hypotheses; do not record it as a finding.

The one exception worth stating out loud: if a measure named by an accountability turns
out to be unusable — missing, leaked, or survivorship-biased — that *is* a real result. It
means the role cannot currently be held to that accountability on this data. Say so
plainly in your summary; it is more valuable than a weak effect found elsewhere.
