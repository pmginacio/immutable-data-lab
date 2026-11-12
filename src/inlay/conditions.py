from collections.abc import Iterable
from typing import TYPE_CHECKING

from inlay.meta import FieldMeta, meta
from inlay.nodes import (
    ArithmeticOp,
    BinOp,
    Call,
    Compare,
    CompareOp,
    Condition,
    Filter,
    In,
    Match,
    Table,
    Value,
    result_type,
    value_fields,
)

if TYPE_CHECKING:
    from inlay.functions import Function


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
            raise AttributeError(f"{_describe(ref)} is not a Ref, so it has no field {name!r}")
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
        return self.__arithmetic("+", self.__value, self.__operand(other))

    def __radd__(self, other: object) -> Expr:
        return self.__arithmetic("+", self.__operand(other), self.__value)

    def __sub__(self, other: object) -> Expr:
        return self.__arithmetic("-", self.__value, self.__operand(other))

    def __rsub__(self, other: object) -> Expr:
        return self.__arithmetic("-", self.__operand(other), self.__value)

    def __mul__(self, other: object) -> Expr:
        return self.__arithmetic("*", self.__value, self.__operand(other))

    def __rmul__(self, other: object) -> Expr:
        return self.__arithmetic("*", self.__operand(other), self.__value)

    def __truediv__(self, other: object) -> Expr:
        return self.__arithmetic("/", self.__value, self.__operand(other))

    def __rtruediv__(self, other: object) -> Expr:
        return self.__arithmetic("/", self.__operand(other), self.__value)

    def startswith(self, prefix: str) -> Condition:
        relations, field = _field_parts(self)
        return _through(relations, Match(field, "startswith", prefix))

    def endswith(self, suffix: str) -> Condition:
        relations, field = _field_parts(self)
        return _through(relations, Match(field, "endswith", suffix))

    def __compare(self, op: CompareOp, other: object) -> Condition:
        return _through(self.__relations, Compare(self.__value, op, self.__operand(other)))

    def __arithmetic(self, op: ArithmeticOp, left: object, right: object) -> Expr:
        for side in (left, right):
            if result_type(side) not in (int, float):
                raise TypeError(f"arithmetic needs numbers, got {_describe(side)}")
        return Expr(self.__relations, BinOp(op, left, right))

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


def one_of[T](field: T, values: Iterable[T]) -> Condition:
    relations, f = _field_parts(field)
    return _through(relations, In(f, tuple(values)))


def apply(fn: Function, x: Expr) -> Expr:
    """`fn` applied to the value of `x`, which must be of the type `fn` takes."""
    relations, value = _parts(x)
    if not issubclass(result_type(value), fn.arg):
        raise TypeError(f"{fn.name} needs a {fn.arg.__name__}, got {_describe(value)}")
    return Expr(relations, Call(fn, value))


def as_own_field(cls: type, value: object) -> FieldMeta:
    """The field `value` stands for, which must be a field of `cls` itself.

    `sql.select_sql` and `sql.aggregate_sql` use it as a plain column of the table; a related field would need a join.
    """
    relations, field = _field_parts(value)
    if relations or field.owner is not cls:
        raise ValueError(f"{field.owner.__name__}.{field.name} is not a field of {cls.__name__}")
    return field


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
        raise TypeError(f"expected a field such as Order.total, got {_describe(field)}")
    return relations, field


def _describe(value: object) -> str:
    match value:
        case FieldMeta():
            return f"{value.owner.__name__}.{value.name}"
        case Call() | BinOp():
            return "a computed value"
        case _:
            return repr(value)
