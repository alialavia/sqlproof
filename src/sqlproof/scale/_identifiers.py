"""The one rule for identifiers this package interpolates into SQL.

Bare, unquoted SQL identifier: a letter or underscore, then letters,
digits or underscores. Deliberately conservative -- this rejects some
identifiers Postgres would itself accept (quoted identifiers with spaces
or special characters), which is the right trade here: these strings are
interpolated straight into generated SQL -- column references by the
argument resolvers (`args.py`), the function name by the probe
(`probe.py`) -- and catalog-driven discovery is on this feature's
roadmap, at which point they stop being developer literals and start
coming from introspection of the user's own database.

Matched with `fullmatch`, not `match` against a `$`-anchored pattern: in
Python, `$` matches immediately before a trailing "\\n" as well as at the
true end of string, so "id\\n" would otherwise pass as a bare identifier
and carry one newline into the generated SQL. `fullmatch` has no such
carve-out.
"""

from __future__ import annotations

import re

from sqlproof.exceptions import SqlProofUsageError

IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_function_name(function: str) -> None:
    """Raise `SqlProofUsageError` unless every dot-separated segment of
    `function` is a bare SQL identifier (`fn`, `schema.fn`).

    The probe interpolates the name unquoted into `SELECT {function}(...)`,
    so Postgres folds it to lower case exactly as it would in hand-written
    SQL. Quoting it with `sql.Identifier` instead would make a mixed-case
    name case-sensitive. Validating keeps that behaviour while refusing
    anything that is not a name: unchecked, the name
    `s.noop(); COMMIT; DELETE FROM s.canary; SELECT s.noop` runs the
    DELETE and commits it.
    """
    for segment in function.split("."):
        if not IDENTIFIER_RE.fullmatch(segment):
            msg = (
                f"Function name {function!r} contains an invalid identifier "
                f"segment {segment!r}. Each dot-separated part must be a bare "
                "SQL identifier (letters, digits and underscores, not starting "
                "with a digit) -- the name is interpolated directly into the "
                "probe's SQL. A function with a quoted or exotic name must be "
                "renamed, or wrapped in one with a bare name."
            )
            raise SqlProofUsageError(msg)
