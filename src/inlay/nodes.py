from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from inlay.functions import Aggregate, Function
from inlay.meta import EntityMeta, FieldMeta, meta

type CompareOp = Literal["==", "!=", "<", "<=", ">", ">="]
type ArithmeticOp = Literal["+", "-", "*", "/"]


@dataclass(frozen=True)
class Call:
    fn: Function[Any]
    args: tuple[object, ...]  # `Value`s of the record at hand, or plain values


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
    """The value of the group of `groups` whose keys equal `at`, values of the record at hand; None if there is none.

    `groups` is a `Group`, or a query of one that filters, sorts or slices the groups.
    """

    groups: Node
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


@dataclass(frozen=True)
class Group:
    """`aggregate` of `value` over the records of `source` with each value of `keys`, one row for each.

    `m` describes the rows, with a column for each key, then one for the value, so they can be filtered and sorted.
    """

    source: Node
    aggregate: Aggregate[Any]
    value: Value | None
    keys: tuple[Value, ...]
    m: EntityMeta


def group(source: Node, aggregate: Aggregate[Any], value: Value | None, keys: tuple[Value, ...]) -> Group:
    # The rows of each group are of a class of their own, so only their columns can be used on them.
    cls = type(f"groups of {record_meta(source).cls.__name__}", (), {})
    columns = [FieldMeta(cls, GROUP_KEY.format(i), result_type(k), is_key=True) for i, k in enumerate(keys)]
    value_type = aggregate.result_type(None if value is None else result_type(value))
    columns.append(FieldMeta(cls, GROUP_VALUE, value_type, optional=True))
    return Group(source, aggregate, value, keys, EntityMeta(cls, "", tuple(columns), ()))


GROUP_KEY = "_inlay_key{}"
GROUP_VALUE = "_inlay_value"


def root_group(node: Node) -> Group:
    """The `Group` under the filters, sorts and slices of `node`."""
    while not isinstance(node, Group):
        assert isinstance(node, Filter | Sort | Slice)
        node = node.source
    return node


def group_value(node: Node) -> FieldMeta:
    """The value column of the groups of `node`."""
    return root_group(node).m.fields[-1]


type Node = Table | Filter | Sort | Slice | Project | Group


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
    return And(tuple(map(as_condition, conds)))


def any_of(*conds: Condition | bool) -> Or:
    return Or(tuple(map(as_condition, conds)))


def negate(cond: Condition | bool) -> Not:
    return Not(as_condition(cond))


# Type checkers see `Order.total > 100` as a `bool`, so a stray real `bool` must be caught at runtime.
def as_condition(value: object) -> Condition:
    if not isinstance(value, Condition):
        raise TypeError(f"expected a condition such as Order.total > 100, got {value!r}")
    return value


def check_condition(cls: type, cond: Condition) -> None:
    """Raise if `cond` tests a field that is not a field of `cls`."""
    for field in condition_fields(cond):
        if field.owner is not cls:
            raise ValueError(f"{field.owner.__name__}.{field.name} cannot be used on {cls.__name__}")


def record_meta(query: Node) -> EntityMeta:
    """What the records of `query` are: an entity, or a projection."""
    match query:
        case Table(cls):
            return meta(cls)
        case Project(_, projection):
            return projection.m
        case Group(m=m):
            return m
        case Filter(source) | Sort(source) | Slice(source):
            return record_meta(source)


def classes_read(node: Node | Condition) -> set[type]:
    """The classes whose tables are read to run `node`, including those reached through `In`."""
    match node:
        case Table(cls):
            return {cls}
        case Filter(source, cond):
            # The fields `cond` tests are of the records of `source`, a projection or groups rather than a table.
            return classes_read(source) | (classes_read(cond) - {record_meta(source).cls})
        case Sort(source, keys):
            return classes_read(source).union(*map(_classes_reached, keys))
        case Project(source, projection):
            return classes_read(source).union(*map(_classes_reached, projection.values))
        case Slice(source):
            return classes_read(source)
        case Group(source, _, value, keys):
            return classes_read(source).union(*map(_classes_reached, (value, *keys)))
        case Compare(left, _, right):
            return {f.owner for f in condition_fields(node)} | _classes_reached(left) | _classes_reached(right)
        case In(field, Filter() as records):
            return {field.owner} | classes_read(records)
        case And(items) | Or(items):
            return set().union(*map(classes_read, items))
        case Not(item):
            return classes_read(item)
        case _:
            return {f.owner for f in condition_fields(node)}


def _classes_reached(value: object) -> set[type]:
    """The classes whose records `value` reads besides the record at hand: through `Ref`s, or in the groups it looks up."""
    match value:
        case RefValue(refs):
            return {r.target for r in refs if r.target is not None}
        case GroupValue(groups, at):
            return classes_read(groups).union(*map(_classes_reached, at))
        case Call(_, args):
            return set().union(*map(_classes_reached, args))
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
        case Call(fn, args):
            return fn.result_type(tuple(result_type(a) for a in args))
        case BinOp(op, left, right):
            types = {_number_type(left), _number_type(right)}
            return float if op == "/" or float in types else int
        case RefValue(_, inner):
            return result_type(inner)
        case GroupValue(groups):
            return group_value(groups).py_type
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
        case Call(_, args):
            for v in args:
                yield from value_fields(v)
        case BinOp(_, left, right):
            yield from value_fields(left)
            yield from value_fields(right)
        case GroupValue(at=at):
            for v in at:
                yield from value_fields(v)


def replaced(node: Any, swaps: tuple[tuple[Value, Value], ...]) -> Any:
    """`node`, a condition or a value, with each value equal to the first of a pair in `swaps` changed to the second."""
    for old, new in swaps:
        if node == old:
            return new
    match node:
        case Compare(left, op, right):
            return Compare(replaced(left, swaps), op, replaced(right, swaps))
        case Match(field, kind, text):
            return Match(replaced(field, swaps), kind, text)
        case In(field, values):
            return In(replaced(field, swaps), values)
        case And(items) | Or(items):
            return type(node)(tuple(replaced(item, swaps) for item in items))
        case Not(item):
            return Not(replaced(item, swaps))
        case Call(fn, args):
            return Call(fn, tuple(replaced(v, swaps) for v in args))
        case BinOp(op, left, right):
            return BinOp(op, replaced(left, swaps), replaced(right, swaps))
        case _:
            return node


def condition_fields(cond: Condition) -> Iterator[FieldMeta]:
    """The fields of the tested record that `cond` uses, not those of the records it reaches through `In`."""
    match cond:
        case Compare(left, _, right):
            yield from value_fields(left)
            yield from value_fields(right)
        case Match(field, _, _) | In(field, _):
            yield field
        case And(items) | Or(items):
            for item in items:
                yield from condition_fields(item)
        case Not(item):
            yield from condition_fields(item)
