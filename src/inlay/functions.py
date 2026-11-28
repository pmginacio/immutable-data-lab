import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from types import NoneType
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast, get_args, overload, runtime_checkable

if TYPE_CHECKING:
    from inlay.conditions import Expr


@runtime_checkable
class Applicable(Protocol):
    """A value that a `Function` is applied to by building a node, such as the `Expr` of `Order.placed`."""

    def _apply(self, fn: Function[Any], args: tuple[object, ...]) -> Any: ...


@dataclass(frozen=True)
class Function[R]:
    """A function of values: computed in Python on plain values, and by the db when a field is among them.

    `args` is the type of each argument, as `(date,)` or `(str, int, int)`, and `(int, ...)` takes any number
    of `int`s, as `tuple[int, ...]` says.
    A type variable stands for the same type wherever it appears, as in Python's generics: with
    `T = TypeVar("T")`, arguments `(T, T)` and result `T` give the type of the two arguments.
    `sql` is the SQL that computes it, with `{}` for each argument, or with one `{}` for all of them, joined
    by commas, when it takes any number.

    In the db, given None for an argument it takes without None, it gives None, as SQL's functions do.
    A function that takes None, as `coalesce` does, gives None only when all its arguments are None.
    """

    name: str
    args: tuple[object, ...]  # types, unions, type variables, and `...`
    result: type[R] | TypeVar
    python: Callable[..., R]
    sql: str

    # Given a field first, it is a value for queries, such as `to_month(Order.placed)`; given plain values, it is computed.
    @overload
    def __call__(self, first: Expr[Any], /, *rest: object) -> Expr[R]: ...
    @overload
    def __call__(self, *args: object) -> R: ...
    def __call__(self, *args: object) -> Any:
        if applicable := next((a for a in args if isinstance(a, Applicable)), None):
            return applicable._apply(self, args)
        self.result_type(tuple(type(a) for a in args))
        return self.python(*args)

    def result_type(self, types: tuple[type, ...]) -> type:
        """The type the function gives for arguments of `types`. Raises if they are not what it takes."""
        takes = self.args[:1] * len(types) if self.args[-1] is Ellipsis else self.args
        if len(types) != len(takes):
            raise TypeError(f"{self.name} takes {len(takes)} arguments, got {len(types)}")
        found: dict[TypeVar, set[type]] = defaultdict(set)
        for given, taken in zip(types, takes):
            if not _accepts(taken, given):
                raise TypeError(f"{self.name} needs a {_name(taken)}, got a {given.__name__}")
            for var in _type_vars(taken):
                if given is not NoneType:
                    found[var].add(given)
        if not isinstance(self.result, TypeVar):
            return self.result
        return _one_type(self.name, found[self.result])

    def takes_none(self) -> bool:
        return any(NoneType in get_args(t) for t in self.args)

    def sql_of(self, args: list[str]) -> str:
        """The SQL of the function of arguments whose SQL is `args`."""
        return self.sql.format(", ".join(args)) if self.args[-1] is Ellipsis else self.sql.format(*args)


def _accepts(taken: object, given: type) -> bool:
    """Whether a value of type `given` can be passed where `taken` is declared: a type, a union, or a
    type variable, which takes the types it is constrained to, those of its bound, or any."""
    if isinstance(taken, TypeVar):
        if taken.__constraints__:
            return any(issubclass(given, c) for c in taken.__constraints__)
        return issubclass(given, taken.__bound__ or object)
    if parts := get_args(taken):
        return any(_accepts(part, given) for part in parts)
    return issubclass(given, cast(type, taken))


def _type_vars(taken: object) -> list[TypeVar]:
    return [taken] if isinstance(taken, TypeVar) else [t for t in get_args(taken) if isinstance(t, TypeVar)]


def _one_type(name: str, types: set[type]) -> type:
    """The one type of values of `types`: a `float` for `int`s and `float`s, and None's type for none."""
    if not types:
        return NoneType
    if types == {int, float}:
        return float
    if len(types) != 1:
        raise TypeError(f"{name} needs values of one type, got {', '.join(sorted(t.__name__ for t in types))}")
    return next(iter(types))


def _name(t: object) -> str:
    return getattr(t, "__name__", str(t))


@dataclass(frozen=True)
class Aggregate[R]:
    """A function of many values: computed in Python on an iterable, and by the db over the records of a view.

    `sql` is the SQL that computes it, with `{}` where the SQL of the value goes.
    `arg` is the type of the values it takes, or None when it takes no value and counts records.
    `result` is the type it gives; a type variable that is also `arg` gives the type of the values it takes.
    """

    name: str
    python: Callable[[Iterable[Any]], R]
    sql: str
    arg: object | None
    result: type | TypeVar

    def __call__(self, values: Iterable[Any]) -> R:
        return self.python(values)

    def takes(self, value_type: type) -> bool:
        return self.arg is not None and _accepts(self.arg, value_type)

    def result_type(self, value_type: type | None) -> type:
        """The type it gives over values of `value_type`, None when it takes no value."""
        return cast(type, value_type) if isinstance(self.result, TypeVar) else self.result


# Dates are stored as ISO 8601 text, which is also what SQLite's date functions read and write.
to_month = Function("to_month", (date,), date, lambda x: date(x.year, x.month, 1), "strftime('%Y-%m-01', {})")
to_day = Function("to_day", (date,), date, lambda x: date(x.year, x.month, x.day), "date({})")

_T = TypeVar("_T")
_Number = TypeVar("_Number", int, float)

# The first value that is not None, as SQL's COALESCE, which skips only NULL, so a False still wins.
_COALESCE = Function(
    "coalesce",
    (_T | None, ...),
    _T,
    lambda *values: next((v for v in values if v is not None), None),
    "COALESCE({})",
)


# A function of its own only to be typed: a `Function` cannot say that its result is the type of its arguments.
# A field that can be None with a plain value after it, the common case, can no longer be None.
@overload
def coalesce[V](first: Expr[V | None], default: V, /) -> Expr[V]: ...
@overload
def coalesce[V](first: Expr[V], /, *rest: V | Expr[V] | None) -> Expr[V]: ...
@overload
def coalesce[V](*values: V | None) -> V | None: ...
def coalesce(*values: Any) -> Any:
    """The first of `values` that is not None, as SQL's `COALESCE`: computed here for plain values, and a value for
    queries when the first is a field, as `coalesce(Order.customer.city, "unknown")`."""
    return _COALESCE(*values)

# The aggregates the methods of a value use, such as `Order.amount.sum()`.
# SQL gives NULL for the sum of no values, and Python 0.
SUM = Aggregate("sum", sum, "coalesce(SUM({}), 0)", arg=_Number, result=_Number)
MIN = Aggregate("min", min, "MIN({})", arg=_T, result=_T)
MAX = Aggregate("max", max, "MAX({})", arg=_T, result=_T)
# COUNT of a value skips NULL, as counting the values that are not None does.
COUNT = Aggregate("count", lambda values: sum(1 for v in values if v is not None), "COUNT({})", arg=_T, result=int)
# SQLite's AVG always gives a float, as `fmean` does.
avg = Aggregate("avg", statistics.fmean, "AVG({})", arg=_Number, result=float)

# The built-ins `aggregate_by` takes, so its users write `sum` rather than an inlay name.
_BUILTIN_AGGREGATES: dict[object, Aggregate[Any]] = {
    len: Aggregate("len", lambda values: sum(1 for _ in values), "COUNT(*)", arg=None, result=int),
    sum: SUM,
    min: MIN,
    max: MAX,
}


def aggregate_of(func: object) -> Aggregate[Any]:
    """`func` as an `Aggregate`: itself, or the one a built-in such as `sum` stands for."""
    if isinstance(func, Aggregate):
        return func
    if aggregate := _BUILTIN_AGGREGATES.get(func):
        return aggregate
    raise TypeError(f"expected len, sum, min, max or an Aggregate such as avg, got {func!r}")
