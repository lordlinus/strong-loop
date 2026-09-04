"""questions.py — what the role is accountable for, as questions the agent inherits.

One question per accountability, derived from the charter and nothing else. There is no
heuristic compiler here and the agent does not get to choose: the charter says what the
role owns, and the loop asks about exactly that. Anything cleverer belongs in a later step,
once this has proved too blunt.
"""

from __future__ import annotations

from .charter import RoleCharter
from .types import Question

_VERB = {"increase": "raise", "decrease": "lower", "stabilise": "stabilise"}


def questions_from(charter: RoleCharter) -> list[Question]:
    return [
        Question(
            accountability_id=acc.id,
            text=(
                f"Which segments or drivers most {_VERB[acc.direction]} `{acc.metric}`, "
                f"and by how much? ({acc.statement})"
            ),
            why_it_matters=(
                f"{charter.role} is accountable for this over {acc.horizon_days} days; a "
                f"finding here is one the role can act on."
            ),
            target_measure=acc.metric,
            priority=acc.priority,
        )
        for acc in sorted(charter.accountabilities, key=lambda a: a.priority)
    ]
