---
name: choosing-a-gate
description: Use before calling test_hypothesis when unsure which gate a claim needs or how to write its spec. Maps the shape of a question to a gate, and gives the pandas expression rules that cause most refusals.
---

# Choosing a gate

A gate is a *shape of question*, not a topic. Pick by the shape of the claim you are
making, never by the subject matter. `list_gates` gives the current registry and each
gate's `spec_schema`; this skill is how to choose between them and how to write the spec
so it is not refused.

## Matching the claim to the gate

| Your claim | Gate |
|---|---|
| "This subgroup has a higher **rate** of a yes/no outcome" | `proportion_lift` |
| "This subgroup has a different **average** of a continuous measure" | `mean_shift` |
| "This attribute drives the outcome **even after** controlling for something else" | `driver_effect` |

If the outcome is 0/1 or true/false, it is a rate — use `proportion_lift`. If it is an
amount, a duration or a score, use `mean_shift`. Sending a continuous measure to
`proportion_lift` is refused, not coerced.

## Writing `where`

`where` is a **pandas** boolean expression over bare column names. This is the single
largest source of refusals, and all of it is mechanical:

```
GOOD   (tenure_years > 5) & (region == 'north')
GOOD   (complexity_score >= 3) | (prior_claims > 0)
GOOD   ~(channel == 'digital')

BAD    tenure_years > 5 AND region = 'north'      SQL. Refused.
BAD    tenure_years > 5 && region == 'north'      SQL. Refused.
BAD    df[df.tenure > 5]                          Subscript/attribute. Refused.
BAD    tenure_years.between(1, 5)                 Function call. Refused.
```

Rules that are enforced, not advisory:

- `&` `|` `~` for and/or/not. Never `AND`, `OR`, `NOT`, `&&`, `||`.
- **Parenthesise every comparison.** `&` binds tighter than `>`, so an unparenthesised
  expression silently evaluates something you did not ask.
- `==` for equality, never `=`.
- Bare column names only. No function calls, no attribute access, no indexing.

## Getting a usable subgroup

- **Too small → INCONCLUSIVE.** This means underpowered, *not* false. Coarsen: widen a
  numeric threshold, drop one clause from the conjunction, or group at a level above.
- **Everything or nothing → refused as degenerate.** A rule matching every row or no row
  makes no comparison. Check the level first in `get_data_profile`.
- Start with a **single-clause** rule. Add a second clause only when the first is
  established. Three-clause rules on a first attempt are how iterations get wasted.

## Before you test anything

Read the outcome's `base_rate`/`mean` in `get_data_profile`. Without the baseline you cannot tell whether a
subgroup rate of 12% is remarkable or ordinary, and you will spend gate budget finding
out what one call would have told you.

## Committing

Write the `statement` before you look at the result, and make it falsifiable — name the
subgroup, the measure and the direction. `rationale` should say why you expect it. You
cannot revise either after seeing the verdict, and a statement vague enough to survive
any outcome has not tested anything.
