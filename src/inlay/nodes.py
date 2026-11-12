from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from inlay.meta import FieldMeta

if TYPE_CHECKING:
    from inlay.functions import Function

type CompareOp = Literal["==", "!=", "<", "<=", ">", ">="]
type ArithmeticOp = Literal["+", "-", "*", "/"]


@dataclass(frozen=True, slots=True)
class Call:
    fn: Function[Any, Any]
    arg: Value


@dataclass(frozen=True, slots=True)
class BinOp:
    op: ArithmeticOp
    left: object  # a `Value`, or a number
    right: object


type Value = FieldMeta | Call | BinOp


@dataclass(frozen=True, slots=True)
class Table:
    cls: type


@dataclass(frozen=True, slots=True)
class Filter:
    source: Table
    cond: Condition


class Condition:
    """A test on a record.

    Type checkers see a comparison like `Order.total > 100` as a `bool`,
    so the functions that take conditions accept `Condition | bool`.
    """

    __slots__ = ()

    def __and__(self, other: Condition | bool) -> And:
        return all_of(self, other)

    def __or__(self, other: Condition | bool) -> Or:
        return any_of(self, other)

    def __bool__(self) -> bool:
        raise TypeError(
            "a condition has no truth value: use all_of or &, any_of or |, and negate instead of "
            "and, or and not; use one_of instead of in; write 1 < x < 5 as (x > 1) & (x < 5)"
        )


@dataclass(frozen=True, slots=True)
class Compare(Condition):
    left: Value
    op: CompareOp
    right: object  # a plain value, or a `Value` of the same record


@dataclass(frozen=True, slots=True)
class Match(Condition):
    field: FieldMeta
    kind: Literal["startswith", "endswith", "contains"]
    text: str


@dataclass(frozen=True, slots=True)
class In(Condition):
    """The field's value is one of `values`, or the key of one of the records in `values`."""

    field: FieldMeta
    values: tuple[object, ...] | Filter


@dataclass(frozen=True, slots=True)
class And(Condition):
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Or(Condition):
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Not(Condition):
    item: Condition


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


def check_condition(cls: type, cond: Condition) -> None:
    """Raise if `cond` tests a field that is not a field of `cls`."""
    for field in _fields(cond):
        if field.owner is not cls:
            raise ValueError(f"{field.owner.__name__}.{field.name} cannot be used on {cls.__name__}")


def classes_read(cond: Condition) -> set[type]:
    """The classes whose tables are read to test `cond`: the one it applies to and those it reaches through `In`."""
    match cond:
        case In(field, Filter(Table(cls), inner)):
            return {field.owner, cls} | classes_read(inner)
        case And(items) | Or(items):
            return set().union(*map(classes_read, items))
        case Not(item):
            return classes_read(item)
        case _:
            return {f.owner for f in _fields(cond)}


def result_type(value: object) -> type:
    """The Python type that `value` gives for a record: a `Value`, or a plain value such as `100`."""
    match value:
        case FieldMeta():
            return value.py_type
        case Call(fn, _):
            return fn.result
        case BinOp("/", _, _):
            return float
        case BinOp(_, left, right):
            return float if float in (result_type(left), result_type(right)) else int
        case _:
            return type(value)


def value_fields(value: object) -> Iterator[FieldMeta]:
    """The fields that `value` reads; none for a plain value."""
    match value:
        case FieldMeta():
            yield value
        case Call(_, arg):
            yield from value_fields(arg)
        case BinOp(_, left, right):
            yield from value_fields(left)
            yield from value_fields(right)


def _fields(cond: Condition) -> Iterator[FieldMeta]:
    """The fields of the tested record that `cond` uses, not those of the records it reaches through `In`."""
    match cond:
        case Compare(left, _, right):
            yield from value_fields(left)
            yield from value_fields(right)
        case Match(field, _, _) | In(field, _):
            yield field
        case And(items) | Or(items):
            for item in items:
                yield from _fields(item)
        case Not(item):
            yield from _fields(item)
