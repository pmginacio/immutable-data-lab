from collections.abc import Callable, Iterable

from inlay.meta import FieldMeta, meta
from inlay.nodes import Compare, CompareOp, Condition, Filter, In, Match, Table


class FieldRef:
    """A field as written on an entity class: `Order.total`, or `Order.customer.city` through a relation.

    The steps are the fields followed: each `Ref` field on the way, then the field itself.
    Comparing it builds a condition, so it has no equality or hash of its own.
    """

    # Name-mangled so it never hides a field of the class a `Ref` points to.
    __slots__ = ("__steps",)

    def __init__(self, steps: tuple[FieldMeta, ...]) -> None:
        self.__steps = steps

    def __getattr__(self, name: str) -> FieldRef:
        last = self.__steps[-1]
        if last.target is None:
            raise AttributeError(f"{last.owner.__name__}.{last.name} is not a Ref, so it has no field {name!r}")
        return FieldRef((*self.__steps, meta(last.target).field(name)))

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

    def startswith(self, prefix: str) -> Condition:
        return _on_steps(self.__steps, lambda field: Match(field, "startswith", prefix))

    def endswith(self, suffix: str) -> Condition:
        return _on_steps(self.__steps, lambda field: Match(field, "endswith", suffix))

    def __compare(self, op: CompareOp, other: object) -> Condition:
        if isinstance(other, FieldRef):
            if _record(other.__steps) != _record(self.__steps):
                raise ValueError("only two fields of the same record can be compared")
            other = other.__steps[-1]
        return _on_steps(self.__steps, lambda field: Compare(field, op, other))


def contains(field: str, text: str) -> Condition:
    return _on_steps(_steps(field), lambda f: Match(f, "contains", text))


def one_of[T](field: T, values: Iterable[T]) -> Condition:
    values = tuple(values)
    return _on_steps(_steps(field), lambda f: In(f, values))


def as_own_field(cls: type, value: object) -> FieldMeta:
    """The field `value` stands for, which must be a field of `cls` itself.

    `sql.select_sql` and `sql.aggregate_sql` use it as a plain column of the table; a related field would need a join.
    """
    *via, last = _steps(value)
    if via or last.owner is not cls:
        raise ValueError(f"{last.owner.__name__}.{last.name} is not a field of {cls.__name__}")
    return last


def _on_steps(steps: tuple[FieldMeta, ...], build: Callable[[FieldMeta], Condition]) -> Condition:
    """The condition `build` makes on the last field, as a condition on the record of the first one.

    Each relation on the way becomes an `In`: the `Ref` field's key is one of the records that match.
    """
    *relations, last = steps
    cond = build(last)
    for ref in reversed(relations):
        assert ref.target is not None
        cond = In(ref, Filter(Table(ref.target), cond))
    return cond


def _record(steps: tuple[FieldMeta, ...]) -> tuple[tuple[FieldMeta, ...], type]:
    """Which record the last field belongs to: the relations that reach it and its class."""
    return steps[:-1], steps[-1].owner


# Type checkers see `Order.total` as an `int`, so the functions that take a field are typed with its value type.
def _steps(value: object) -> tuple[FieldMeta, ...]:
    if not isinstance(value, FieldRef):
        raise TypeError(f"expected a field such as Order.total, got {value!r}")
    # The slot is name-mangled, so outside the class it is only reachable by its mangled name.
    return getattr(value, "_FieldRef__steps")
