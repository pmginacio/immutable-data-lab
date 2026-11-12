from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, cast, runtime_checkable


@runtime_checkable
class Applicable(Protocol):
    """A value that a `Function` is applied to by building a node, such as the `Expr` of `Order.placed`."""

    def apply(self, fn: Function[Any, Any]) -> Any: ...


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
        if isinstance(x, self.arg):
            return self.python(x)
        if isinstance(x, Applicable):
            return cast(R, x.apply(self))
        raise TypeError(f"{self.name} needs a {self.arg.__name__}, got {x!r}")


# Dates are stored as ISO 8601 text, which is also what SQLite's date functions read and write.
to_month = Function("to_month", date, date, lambda x: date(x.year, x.month, 1), "strftime('%Y-%m-01', {})")
to_day = Function("to_day", date, date, lambda x: date(x.year, x.month, x.day), "date({})")
