from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from inlay.functions import Aggregate, Function
from inlay.meta import EntityMeta, FieldMeta, meta

type CompareOp = Literal["==", "!=", "<", "<=", ">", ">="]
type ArithmeticOp = Literal["+", "-", "*", "/"]


@dataclass(frozen=True)
class Call:
    fn: Function[Any, Any]
    arg: Value


@dataclass(frozen=True)
class BinOp:
    op: ArithmeticOp
    left: object  # a `Value`, or a number
    right: object


@dataclass(frozen=True)
class RefValue:
    """`value`, read on the record reached by following `refs`, the `Ref` fields in order from the record at hand."""

    refs: tuple[FieldMeta, ...]
    value: Value


@dataclass(frozen=True)
class GroupValue:
    """`aggregate` of `value` over the records of `source` whose `keys` equal `at`, values of the record at hand.

    It is what `aggregate_by` gives for those keys, and None when no record of `source` has them.
    """

    source: Node
    aggregate: Aggregate[Any]
    value: Value | None
    keys: tuple[Value, ...]
    at: tuple[Value, ...]


type Value = FieldMeta | Call | BinOp | RefValue | GroupValue


@dataclass(frozen=True)
class Table:
    cls: type


@dataclass(frozen=True)
class Filter:
    source: Node
    cond: Condition


@dataclass(frozen=True)
class Sort:
    """`source` sorted by `keys`, like the built-in `sorted`: ties keep the order of `source`."""

    source: Node
    keys: tuple[Value, ...]
    reverse: bool


@dataclass(frozen=True)
class Slice:
    source: Node
    start: int
    stop: int | None


@dataclass(frozen=True)
class Projection:
    """A read-only shape of the records of `root`: `m` describes its fields, and `values` gives each one's value."""

    root: type
    m: EntityMeta
    values: tuple[Value, ...]


@dataclass(frozen=True)
class Project:
    """The records of `source`, a filtered view of the root of `projection`, in the shape of the projection."""

    source: Node
    projection: Projection


type Node = Table | Filter | Sort | Slice | Project


class Condition:
    """A test on a record.

    Type checkers see a comparison like `Order.total > 100` as a `bool`,
    so the functions that take conditions accept `Condition | bool`.
    """

    def __and__(self, other: Condition | bool) -> And:
        return all_of(self, other)

    def __or__(self, other: Condition | bool) -> Or:
        return any_of(self, other)

    def __bool__(self) -> bool:
        raise TypeError(
            "a condition has no truth value: use all_of or &, any_of or |, and negate instead of "
            "and, or and not; use one_of instead of in; write 1 < x < 5 as (x > 1) & (x < 5)"
        )


@dataclass(frozen=True)
class Compare(Condition):
    left: Value
    op: CompareOp
    right: object  # a plain value, or a `Value` of the same record


@dataclass(frozen=True)
class Match(Condition):
    field: FieldMeta
    kind: Literal["startswith", "endswith", "contains"]
    text: str


@dataclass(frozen=True)
class In(Condition):
    """The field's value is one of `values`, or the key of one of the records in `values`."""

    field: FieldMeta
    values: tuple[object, ...] | Filter


@dataclass(frozen=True)
class And(Condition):
    items: tuple[Condition, ...]


@dataclass(frozen=True)
class Or(Condition):
    items: tuple[Condition, ...]


@dataclass(frozen=True)
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


def record_meta(query: Node) -> EntityMeta:
    """What the records of `query` are: an entity, or a projection."""
    match query:
        case Table(cls):
            return meta(cls)
        case Project(_, projection):
            return projection.m
        case Filter(source) | Sort(source) | Slice(source):
            return record_meta(source)


def classes_read(node: Node | Condition) -> set[type]:
    """The classes whose tables are read to run `node`, including those reached through `In`."""
    match node:
        case Table(cls):
            return {cls}
        case Filter(source, cond):
            return classes_read(source) | classes_read(cond)
        case Sort(source, keys):
            return classes_read(source).union(*map(_classes_reached, keys))
        case Project(source, projection):
            return classes_read(source).union(*map(_classes_reached, projection.values))
        case Slice(source):
            return classes_read(source)
        case Compare(left, _, right):
            return {f.owner for f in _fields(node)} | _classes_reached(left) | _classes_reached(right)
        case In(field, Filter() as records):
            return {field.owner} | classes_read(records)
        case And(items) | Or(items):
            return set().union(*map(classes_read, items))
        case Not(item):
            return classes_read(item)
        case _:
            return {f.owner for f in _fields(node)}


def _classes_reached(value: object) -> set[type]:
    """The classes whose records `value` reads besides the record at hand: through `Ref`s, or in the groups it looks up."""
    match value:
        case RefValue(refs):
            return {r.target for r in refs if r.target is not None}
        case GroupValue(source, _, inner, keys, at):
            return classes_read(source).union(*map(_classes_reached, (inner, *keys, *at)))
        case Call(_, arg):
            return _classes_reached(arg)
        case BinOp(_, left, right):
            return _classes_reached(left) | _classes_reached(right)
        case _:
            return set()


def result_type(value: object) -> type:
    """The Python type that `value` gives for a record: a `Value`, or a plain value such as `100`.

    Raises if `value` is badly typed, such as a function given the wrong type or arithmetic on text.
    """
    match value:
        case FieldMeta():
            return value.py_type
        case Call(fn, arg):
            if not issubclass(result_type(arg), fn.arg):
                raise TypeError(f"{fn.name} needs a {fn.arg.__name__}, got {describe(arg)}")
            return fn.result
        case BinOp(op, left, right):
            types = {_number_type(left), _number_type(right)}
            return float if op == "/" or float in types else int
        case RefValue(_, inner):
            return result_type(inner)
        case GroupValue(_, aggregate, inner):
            return aggregate.result or result_type(inner)
        case _:
            return type(value)


def _number_type(value: object) -> type:
    t = result_type(value)
    if t not in (int, float):
        raise TypeError(f"arithmetic needs numbers, got {describe(value)}")
    return t


def describe(value: object) -> str:
    match value:
        case FieldMeta():
            return f"{value.owner.__name__}.{value.name}"
        case RefValue(refs, FieldMeta() as field):
            return ".".join([describe(refs[0]), *(r.name for r in refs[1:]), field.name])
        case Call() | BinOp() | RefValue() | GroupValue():
            return "a computed value"
        case _:
            return repr(value)


def value_fields(value: object) -> Iterator[FieldMeta]:
    """The fields of the record at hand that `value` reads; none for a plain value."""
    match value:
        case FieldMeta():
            yield value
        case RefValue(refs):
            yield refs[0]
        case Call(_, arg):
            yield from value_fields(arg)
        case BinOp(_, left, right):
            yield from value_fields(left)
            yield from value_fields(right)
        case GroupValue(at=at):
            for v in at:
                yield from value_fields(v)


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
