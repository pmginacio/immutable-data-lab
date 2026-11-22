from collections.abc import Iterable
from typing import Any

from inlay.functions import Function
from inlay.meta import FieldMeta, meta
from inlay.nodes import (
    BinOp,
    Call,
    Compare,
    CompareOp,
    Condition,
    Filter,
    Group,
    In,
    Match,
    RefValue,
    Table,
    Value,
    condition_fields,
    describe,
    replaced,
    result_type,
    value_fields,
)


class Expr:
    """A value written on entity classes.

    It is a field such as `Order.total`, a field through relations such as `Order.customer.city`,
    or a value computed from fields such as `Order.total * 100` or `to_month(Order.placed)`.
    Comparing it builds a condition, so it has no equality or hash of its own.
    """

    # Name-mangled so they never hide a field of the class a `Ref` points to.
    __slots__ = ("__relations", "__value")

    def __init__(self, relations: tuple[FieldMeta, ...], value: Value) -> None:
        self.__relations = relations  # the `Ref` fields followed to reach the record that `value` is computed from
        self.__value = value

    def __getattr__(self, name: str) -> Expr:
        ref = self.__value
        if not isinstance(ref, FieldMeta) or ref.target is None:
            raise AttributeError(f"{describe(ref)} is not a Ref, so it has no field {name!r}")
        return Expr((*self.__relations, ref), meta(ref.target).field(name))

    def __eq__(self, other: object) -> Condition:  # type: ignore[override]
        return self.__compare("==", other)

    def __ne__(self, other: object) -> Condition:  # type: ignore[override]
        return self.__compare("!=", other)

    def __lt__(self, other: object) -> Condition:
        return self.__compare("<", other)

    def __le__(self, other: object) -> Condition:
        return self.__compare("<=", other)

    def __gt__(self, other: object) -> Condition:
        return self.__compare(">", other)

    def __ge__(self, other: object) -> Condition:
        return self.__compare(">=", other)

    def __add__(self, other: object) -> Expr:
        return self.__with(BinOp("+", self.__value, self.__operand(other)))

    def __radd__(self, other: object) -> Expr:
        return self.__with(BinOp("+", self.__operand(other), self.__value))

    def __sub__(self, other: object) -> Expr:
        return self.__with(BinOp("-", self.__value, self.__operand(other)))

    def __rsub__(self, other: object) -> Expr:
        return self.__with(BinOp("-", self.__operand(other), self.__value))

    def __mul__(self, other: object) -> Expr:
        return self.__with(BinOp("*", self.__value, self.__operand(other)))

    def __rmul__(self, other: object) -> Expr:
        return self.__with(BinOp("*", self.__operand(other), self.__value))

    def __truediv__(self, other: object) -> Expr:
        return self.__with(BinOp("/", self.__value, self.__operand(other)))

    def __rtruediv__(self, other: object) -> Expr:
        return self.__with(BinOp("/", self.__operand(other), self.__value))

    def _apply(self, fn: Function[Any, Any]) -> Expr:
        """`fn` applied to this value. Calling `fn` on an `Expr` comes here, through `functions.Applicable`."""
        return self.__with(Call(fn, self.__value))

    def startswith(self, prefix: str) -> Condition:
        relations, field = _field_parts(self)
        return _through(relations, Match(field, "startswith", prefix))

    def endswith(self, suffix: str) -> Condition:
        relations, field = _field_parts(self)
        return _through(relations, Match(field, "endswith", suffix))

    def __compare(self, op: CompareOp, other: object) -> Condition:
        return _through(self.__relations, Compare(self.__value, op, self.__operand(other)))

    def __with(self, value: Value) -> Expr:
        """`value`, reached through the same relations. Raises if `value` is badly typed."""
        result_type(value)
        return Expr(self.__relations, value)

    def __operand(self, other: object) -> object:
        """`other` as part of a node with this value: a plain value, or the value of an `Expr` of the same record."""
        if not isinstance(other, Expr):
            return other
        if other.__record() != self.__record():
            raise ValueError("only values of the same record can be combined")
        return other.__value

    def __record(self) -> tuple[tuple[FieldMeta, ...], type]:
        """Which record the value is computed from: the relations that reach it and its class."""
        return self.__relations, next(value_fields(self.__value)).owner


def contains(field: str, text: str) -> Condition:
    relations, f = _field_parts(field)
    return _through(relations, Match(f, "contains", text))


def is_none(value: object) -> Condition:
    """`value` is None. The same as `value == None`, which linters flag."""
    relations, v = _parts(value)
    return _through(relations, Compare(v, "==", None))


def one_of[T](field: T, values: Iterable[T]) -> Condition:
    relations, f = _field_parts(field)
    return _through(relations, In(f, tuple(values)))


def own_value(cls: type, value: object) -> Value:
    """The value `value` stands for on a record of `cls`: computed from its fields, or read through its `Ref`s."""
    relations, v = _parts(value)
    own = RefValue(relations, v) if relations else v
    for field in value_fields(own):
        if field.owner is not cls:
            raise ValueError(f"{field.owner.__name__}.{field.name} is not a field of {cls.__name__}")
    return own


def group_column(groups: Group, value: object) -> Value:
    """`value` on the rows of `groups`: their value, or one of their keys written as when grouping, as `Order.customer`."""
    relations, v = _parts(value)
    own = replaced(RefValue(relations, v) if relations else v, _key_columns(groups))
    _check_group_fields(groups, value_fields(own))
    return own


def group_condition(groups: Group, cond: Condition) -> Condition:
    """`cond` on the rows of `groups`, testing their value, or their keys written as when grouping."""
    on_rows = replaced(cond, _key_columns(groups))
    _check_group_fields(groups, condition_fields(on_rows))
    return on_rows


def _key_columns(groups: Group) -> tuple[tuple[Value, Value], ...]:
    return tuple(zip(groups.keys, groups.m.fields))


def _check_group_fields(groups: Group, fields: Iterable[FieldMeta]) -> None:
    for f in fields:
        if f.owner is not groups.m.cls:
            raise ValueError(
                f"{describe(f)} is not a key of these groups; to keep only some records, filter them before aggregate_by"
            )


def record_values(values: tuple[object, ...]) -> tuple[tuple[FieldMeta, ...], tuple[Value, ...]] | None:
    """The relations and values of `values` if they are values of one record, such as `(Ad.town, Ad.rooms)`.

    None if they are plain values, such as `("Faro", 2)`.
    """
    if not any(isinstance(v, Expr) for v in values):
        return None
    parts = [_parts(v) for v in values]
    if len({(relations, next(value_fields(v)).owner) for relations, v in parts}) > 1:
        raise ValueError("only values of the same record can be combined")
    return parts[0][0], tuple(v for _, v in parts)


def _through(relations: tuple[FieldMeta, ...], cond: Condition) -> Condition:
    """`cond`, on the record reached through `relations`, as a condition on the record of the first relation.

    Each relation becomes an `In`: the `Ref` field's key is one of the records that match.
    """
    for ref in reversed(relations):
        assert ref.target is not None
        cond = In(ref, Filter(Table(ref.target), cond))
    return cond


# Type checkers see `Order.total` as an `int`, so the functions that take a field are typed with its value type.
def _parts(value: object) -> tuple[tuple[FieldMeta, ...], Value]:
    if not isinstance(value, Expr):
        raise TypeError(f"expected a field such as Order.total, got {value!r}")
    # The slots are name-mangled, so outside the class they are only reachable by their mangled names.
    return getattr(value, "_Expr__relations"), getattr(value, "_Expr__value")


def _field_parts(value: object) -> tuple[tuple[FieldMeta, ...], FieldMeta]:
    relations, field = _parts(value)
    if not isinstance(field, FieldMeta):
        raise TypeError(f"expected a field such as Order.total, got {describe(field)}")
    return relations, field
