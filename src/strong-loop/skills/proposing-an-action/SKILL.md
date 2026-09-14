---
name: proposing-an-action
description: Use before calling propose_action. Explains the four independent checks in the authority code, what blast radius and observation plan must contain, and why each refusal happens.
---

# Proposing an action

An action is not a recommendation with confidence attached. It is a request for
authority, and `authorise_action` grants it only when four independent conditions hold.
All four are checked in fixed code. None can be argued with.

## The four checks

### 1. The charter names this action type

`get_charter` lists `decision_rights`. If your `action_type` is not one of them, there is
no intent you can write that creates the authority. Pick a granted type or propose
nothing.

### 2. The evidence clears the standard **for that type**

Different actions carry different burdens. A charter may attach a stricter standard to a
costlier action, so evidence that justified one action may not justify another. Every
`finding_id` you cite must be a recorded finding — SUPPORTED evidence that has been
challenged. Citing raw evidence is refused.

If the right requires a causal design, an observational subgroup comparison will not
satisfy it, however small the p-value. Correlational evidence cannot authorise a causal
claim.

### 3. Blast radius is within cap

```json
"blast_radius": {"unit": "<the unit the charter uses>", "max_per_run": 150}
```

The `unit` must match the charter's unit for that action type, and the count must be at
or below its cap. Do not request the cap by default — request what the finding actually
supports. A finding about a subgroup of 300 does not justify acting on 5,000.

### 4. There is a plan to find out whether it worked

```json
"observation_plan": {"metric": "<a metric this role is accountable for>", "lag_days": 90}
```

This is mandatory and it is the check people are surprised by. An action nobody measures
can never be learned from, so the platform refuses to take it. The metric must be one the
role owns; `lag_days` should reflect when the effect could plausibly show up, not when
you would like to know.

## Autonomy is granted, not chosen

The autonomy level comes from the charter's `decision_rights` for that action type. You
do not select it and you cannot raise it. `L1_recommend` produces a proposal for a human;
`L2_act_reversible` and above may execute. A level is earned from the outcome record over
time, not from the strength of one argument.

## Writing it

The report hands each action to a person as: **do this** (`recommendation`), **to these**
(`params.where`, counted by code), **this many** (`blast_radius.max_per_run`), **signed
off by** (the charter's `requires_authority`, if any), **checked by** (`observation_plan`).

- `params.where`: REQUIRED. The pandas rule naming the group, normally the same `where`
  the supporting finding tested. It is screened like a hypothesis and must select at
  least one row; the platform counts the rows and records `target_rows`. Do not restate
  the count in your own key — it is computed.
- `recommendation`: an instruction someone can carry out on that group without asking
  what you meant — the step, who does it, by when. "Investigate further" or "monitor
  closely" is not an action; it is the absence of one.
- `expected_effect`: the direction and rough size you expect, in the observation metric.
  Commit to it. This is what the outcome will later be compared against, and a vague
  expectation makes the outcome uninterpretable.

## When it is refused

Read the reason. It names which of the four checks failed. A refusal is a fact about your
proposal, not an obstacle to work around — rewriting the same action with a softer
description will fail the same check.
