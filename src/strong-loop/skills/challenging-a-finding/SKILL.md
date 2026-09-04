---
name: challenging-a-finding
description: Use immediately after any hypothesis comes back SUPPORTED, before calling record_finding. Gives the four standard ways a real subgroup effect turns out to be an artefact, and the gate spec that tests each one.
---

# Challenging a finding

A SUPPORTED verdict means the effect is unlikely to be noise. It does **not** mean the
attribute you named is the reason. Those are different claims, and only the second one
justifies an action.

Your job now is to try to falsify your own result. If it survives, record it. If it does
not, you have learned something more valuable than the original finding.

## The four challenges, in the order they usually pay off

### 1. Confound — something else travels with your attribute

The most common failure. Your subgroup differs on the outcome *and* on a third variable
that plausibly drives it.

Find a candidate confound among the columns in `get_data_profile`, then re-test your rule with it held
roughly constant:

```
kind: driver_effect
spec: { where: "<your original rule>", target: "<measure>", control: "<candidate confound>" }
```

If the effect collapses once the control is applied, the attribute was a passenger. Say
so and move on.

### 2. Proxy — your attribute is a stand-in for the target

Ask whether the column could only be known *after* the outcome, or is a recoding of it.
A tenure band that is computed at lapse, a status flag set when the claim closes, a
channel recorded at settlement. The charter's `leakage_features` catch the ones somebody
anticipated; this challenge is for the ones nobody did.

The tell: an effect size that is far larger than any real-world mechanism would produce.

### 3. Subgroup concentration — one slice is doing all the work

Split your rule in two along an unrelated column and test each half:

```
kind: proportion_lift
spec: { where: "(<your rule>) & (<split> == '<value A>')", target: "<measure>" }
spec: { where: "(<your rule>) & (<split> == '<value B>')", target: "<measure>" }
```

If it holds in one half and vanishes in the other, your finding is about that half, and
the headline you were about to write would have been wrong for everyone else.

### 4. Arbitrary threshold — the cut point was fitted, not chosen

If your rule uses a numeric cut (`tenure_years > 5`), move it. Test `> 4` and `> 6`. A
real effect degrades gracefully. A mined one disappears the moment the boundary moves,
because the boundary *was* the finding.

## Recording the survivor

`record_finding` refuses evidence that has not been challenged when the charter sets
`requires_confound_control` — which is the default. Challenge **first**, then record, and
put what you tried into the `interpretation`: what you controlled for, where you split,
what held. A finding whose interpretation does not say how it was challenged is not worth
the reader's trust.

## Withdrawing

If a challenge breaks the result, that is a successful iteration, not a wasted one. Do not
record the finding. Do not soften it into a weaker claim and record that instead. The
loop's value is what it declines to believe.
