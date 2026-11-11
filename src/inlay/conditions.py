from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

from inlay.meta import FieldMeta, meta

type CompareOp = Literal["==", "!=", "<", "<=", ">", ">="]


class Condition:
    """A test on a record. Conditions are frozen trees that the compiler turns into SQL.

    Type checkers see a comparison like `Order.total > 100` as a `bool`,
    so the functions that take conditions accept `Condition | bool`.
    """

    __slots__ = ()

    def __and__(self, other: Condition | bool) -> "And":
        return all_of(self, other)

    def __or__(self, other: Condition | bool) -> "Or":
        return any_of(self, other)

    def __bool__(self) -> bool:
        raise TypeError(
            "a condition has no truth value: use all_of or &, any_of or |, and negate instead of "
            "and, or and not; use one_of instead of in; write 1 < x < 5 as (x > 1) & (x < 5)"
        )


# Nodes use eq=False: a generated == would compare their FieldRefs, which builds a condition instead of a bool.


@dataclass(frozen=True, slots=True, eq=False)
class Compare(Condition):
    left: "FieldRef"
    op: CompareOp
    right: object

    def __post_init__(self) -> None:
        # `sql.condition_sql` wraps only the left field's relations in subqueries, so both fields need the same ones.
        if isinstance(self.right, FieldRef) and _record(self.right) != _record(self.left):
            raise ValueError("only two fields of the same record can be compared")


@dataclass(frozen=True, slots=True, eq=False)
class Match(Condition):
    field: "FieldRef"
    kind: Literal["startswith", "endswith", "contains"]
    text: str


@dataclass(frozen=True, slots=True, eq=False)
class OneOf(Condition):
    field: "FieldRef"
    values: tuple[object, ...]


@dataclass(frozen=True, slots=True, eq=False)
class And(Condition):
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True, eq=False)
class Or(Condition):
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True, eq=False)
class Not(Condition):
    item: Condition


class FieldRef:
    """A field, reached from the filtered class through the relations before it.

    The steps are the fields followed: each `Ref` field on the way, then the field itself.
    A reference that ends on a `Ref` field has the fields of the class it points to as attributes.
    Comparing it builds a condition, so it has no equality or hash of its own.
    """

    # Name-mangled so it never hides a field of the class a `Ref` points to.
    __slots__ = ("__steps",)

    def __init__(self, steps: tuple[FieldMeta, ...]) -> None:
        self.__steps = steps

    def __getattr__(self, name: str) -> "FieldRef":
        last = self.__steps[-1]
        if last.target is None:
            raise AttributeError(f"{last.owner.__name__}.{last.name} is not a Ref, so it has no field {name!r}")
        return FieldRef((*self.__steps, meta(last.target).field(name)))

    def __eq__(self, other: object) -> Compare:  # type: ignore[override]
        return Compare(self, "==", other)

    def __ne__(self, other: object) -> Compare:  # type: ignore[override]
        return Compare(self, "!=", other)

    def __lt__(self, other: object) -> Compare:
        return Compare(self, "<", other)

    def __le__(self, other: object) -> Compare:
        return Compare(self, "<=", other)

    def __gt__(self, other: object) -> Compare:
        return Compare(self, ">", other)

    def __ge__(self, other: object) -> Compare:
        return Compare(self, ">=", other)

    def startswith(self, prefix: str) -> Match:
        return Match(self, "startswith", prefix)

    def endswith(self, suffix: str) -> Match:
        return Match(self, "endswith", suffix)


def steps(ref: FieldRef) -> tuple[FieldMeta, ...]:
    # The slot is name-mangled, so outside the class it is only reachable by its mangled name.
    return getattr(ref, "_FieldRef__steps")


def _record(ref: FieldRef) -> tuple[tuple[FieldMeta, ...], type]:
    """Which record a field belongs to: the relations that reach it and its class."""
    *via, last = steps(ref)
    return tuple(via), last.owner


def all_of(*conds: Condition | bool) -> And:
    return And(tuple(map(_as_condition, conds)))


def any_of(*conds: Condition | bool) -> Or:
    return Or(tuple(map(_as_condition, conds)))


def none_of(*conds: Condition | bool) -> Not:
    return Not(any_of(*conds))


def negate(cond: Condition | bool) -> Not:
    return Not(_as_condition(cond))


# Type checkers see `Order.total > 100` as a `bool`, so a stray real `bool` must be caught at runtime.
def _as_condition(value: object) -> Condition:
    if not isinstance(value, Condition):
        raise TypeError(f"expected a condition such as Order.total > 100, got {value!r}")
    return value


def contains(field: str, text: str) -> Match:
    return Match(as_field(field), "contains", text)


def one_of[T](field: T, values: Iterable[T]) -> OneOf:
    return OneOf(as_field(field), tuple(values))


# Type checkers see `Order.total` as an `int`, so the functions that take a field are typed with its value type.
def as_field(value: object) -> FieldRef:
    if not isinstance(value, FieldRef):
        raise TypeError(f"expected a field such as Order.total, got {value!r}")
    return value


def fields(cond: Condition) -> Iterator[FieldRef]:
    """Every field that `cond` tests."""
    match cond:
        case Compare(left, _, FieldRef() as right):
            yield from (left, right)
        case Compare(field, _, _) | Match(field, _, _) | OneOf(field, _):
            yield field
        case And(items) | Or(items):
            for item in items:
                yield from fields(item)
        case Not(item):
            yield from fields(item)


def check_condition(cls: type, cond: Condition) -> None:
    """Raise if `cond` tests a field that cannot be reached from `cls`."""
    for ref in fields(cond):
        path = steps(ref)
        if path[0].owner is not cls:
            raise ValueError(f"{path[-1].owner.__name__}.{path[-1].name} cannot be used on {cls.__name__}")


def as_own_field(cls: type, value: object) -> FieldRef:
    """The field `value` stands for, which must be a field of `cls` itself.

    `sql.select_sql` and `sql.aggregate_sql` use it as a plain column of the table; a related field would need a join.
    """
    ref = as_field(value)
    *via, last = steps(ref)
    if via or last.owner is not cls:
        raise ValueError(f"{last.owner.__name__}.{last.name} is not a field of {cls.__name__}")
    return ref


def classes_read(cond: Condition) -> set[type]:
    """The classes whose tables are read to test `cond`: the one it applies to and those it reaches by relations."""
    return {f.owner for ref in fields(cond) for f in steps(ref)}
