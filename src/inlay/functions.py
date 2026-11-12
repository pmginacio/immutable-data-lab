import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from types import UnionType
from typing import Any, Protocol, cast, runtime_checkable


@runtime_checkable
class Applicable(Protocol):
    """A value that a `Function` is applied to by building a node, such as the `Expr` of `Order.placed`."""

    def _apply(self, fn: Function[Any, Any]) -> Any: ...


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
            return cast(R, x._apply(self))
        raise TypeError(f"{self.name} needs a {self.arg.__name__}, got {x!r}")


@dataclass(frozen=True, slots=True)
class Aggregate[R]:
    """A function of many values: computed in Python on an iterable, and by SQLite over the records of a view.

    `sql` is the SQL that computes it, with `{}` where the SQL of the value goes.
    `arg` is the type of the values it takes, `object` for any, or None when it takes no value and counts records.
    `result` is the type it gives, or None for the type of the values it takes.
    """

    name: str
    python: Callable[[Iterable[Any]], R]
    sql: str
    arg: type | UnionType | None
    result: type | None = None

    def __call__(self, values: Iterable[Any]) -> R:
        return self.python(values)


# Dates are stored as ISO 8601 text, which is also what SQLite's date functions read and write.
to_month = Function("to_month", date, date, lambda x: date(x.year, x.month, 1), "strftime('%Y-%m-01', {})")
to_day = Function("to_day", date, date, lambda x: date(x.year, x.month, x.day), "date({})")

# SQLite's AVG always gives a float, as `fmean` does.
avg = Aggregate("avg", statistics.fmean, "AVG({})", arg=int | float, result=float)

# The built-ins that can be used as aggregates, so users write `sum` rather than an inlay name.
_BUILTIN_AGGREGATES: dict[object, Aggregate[Any]] = {
    len: Aggregate("len", lambda values: sum(1 for _ in values), "COUNT(*)", arg=None, result=int),
    sum: Aggregate("sum", sum, "SUM({})", arg=int | float),
    min: Aggregate("min", min, "MIN({})", arg=object),
    max: Aggregate("max", max, "MAX({})", arg=object),
}


def aggregate_of(func: object) -> Aggregate[Any]:
    """`func` as an `Aggregate`: itself, or the one a built-in such as `sum` stands for."""
    if isinstance(func, Aggregate):
        return func
    if aggregate := _BUILTIN_AGGREGATES.get(func):
        return aggregate
    raise TypeError(f"expected len, sum, min, max or an Aggregate such as avg, got {func!r}")
