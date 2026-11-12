from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import cast

from inlay.conditions import Expr, apply


@dataclass(frozen=True, slots=True)
class Function[A, R]:
    """A function of one value: computed in Python on a plain value, and by SQLite on a field in a condition.

    `sql` is the SQL that computes it, with `{}` where the SQL of the argument goes.
    """

    name: str
    arg: type[A]
    result: type[R]
    python: Callable[[A], R]
    sql: str

    def __call__(self, x: A) -> R:
        if isinstance(x, Expr):
            return cast(R, apply(self, x))
        return self.python(x)


# Dates are stored as ISO 8601 text, which is also what SQLite's date functions read and write.
to_month = Function("to_month", date, date, lambda x: date(x.year, x.month, 1), "strftime('%Y-%m-01', {})")
to_day = Function("to_day", date, date, lambda x: date(x.year, x.month, x.day), "date({})")
