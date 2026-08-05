"""Safe coercion of model-supplied strings into DB enum values.

Why this exists
---------------
Agent tools take free-text filter values straight from the model. When
such a value is compared against a *Postgres enum* column, an invalid
string does not come back as a tidy Python error — asyncpg raises
InvalidTextRepresentationError, and Postgres **aborts the whole
transaction**. Every later statement on that session then fails with
"current transaction is aborted", including the agent runtime's own
attempt to persist the invocation result. The invocation is left stuck
in RUNNING forever with no error recorded.

That is exactly what a model guessing `permitType="LOTO"` (a value that
looks obvious but is spelled ELECTRICAL_LOTO in the enum) used to do to
every RCA run.

So: never let a model-supplied string reach an enum column. Validate it
here first and raise a plain ValueError listing the accepted values. The
agent runtime catches that and feeds it back as a tool error, which the
model can act on — it will usually retry with a correct value — and the
DB session stays clean.
"""

from __future__ import annotations

import enum
from typing import TypeVar

E = TypeVar("E", bound=enum.Enum)


def coerce_enum(value: str, enum_cls: type[E], field_name: str) -> E:
    """Convert a model-supplied string into `enum_cls`, or raise ValueError.

    Matching is case-insensitive and tolerates spaces/hyphens in place of
    underscores, so "hot work" and "Hot-Work" both resolve to HOT_WORK.
    The error message lists every valid value so the model can self-
    correct on its next turn instead of guessing again.
    """
    if isinstance(value, enum_cls):
        return value

    raw = str(value).strip()
    normalised = raw.upper().replace(" ", "_").replace("-", "_")

    for member in enum_cls:
        if member.value.upper() == normalised or member.name.upper() == normalised:
            return member

    valid = ", ".join(m.value for m in enum_cls)
    raise ValueError(
        f"Invalid {field_name}: {raw!r}. Valid values are: {valid}. "
        "Retry with one of these, or omit the filter to search all values."
    )


__all__ = ["coerce_enum"]
