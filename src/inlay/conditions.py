from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal, cast

type Hop = tuple[type, str]
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
        right, left = self.right, self.left
        if isinstance(right, FieldRef) and (right.owner, right.via) != (left.owner, left.via):
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


@dataclass(frozen=True, slots=True, eq=False)
class FieldRef:
    """A field of `owner`, reached from the filtered class through the relations in `via`.

    Comparing it builds a condition, so it has no equality or hash of its own.
    """

    owner: type
    name: str
    via: tuple[Hop, ...]

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


class Path:
    """The object at the end of the relations in `via`; its attributes are that object's fields."""

    # Name-mangled so they never hide a field of the target class.
    __slots__ = ("__target", "__via")

    def __init__(self, target: type, via: tuple[Hop, ...]) -> None:
        object.__setattr__(self, "_Path__target", target)
        object.__setattr__(self, "_Path__via", via)

    def __getattr__(self, name: str) -> "FieldRef | Path":
        node = getattr(self.__target, name)
        if isinstance(node, FieldRef):
            return FieldRef(node.owner, node.name, self.__via + node.via)
        if isinstance(node, Path):
            return Path(node.__target, self.__via + node.__via)
        raise AttributeError(f"{self.__target.__name__} has no field {name!r}")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Path is immutable")

    # Comparing a path with a key is the same as comparing the `Ref` field that ends it.
    def __eq__(self, other: object) -> Compare:  # type: ignore[override]
        return self.__inlay_field__() == other

    def __ne__(self, other: object) -> Compare:  # type: ignore[override]
        return self.__inlay_field__() != other

    # A dunder name, so it never hides a field of the target class.
    def __inlay_field__(self) -> FieldRef:
        """The `Ref` field that ends this path."""
        *via, (owner, name) = self.__via
        return FieldRef(owner, name, tuple(via))


def all_of(*conds: Condition | bool) -> And:
    return And(cast(tuple[Condition, ...], conds))


def any_of(*conds: Condition | bool) -> Or:
    return Or(cast(tuple[Condition, ...], conds))


def none_of(*conds: Condition | bool) -> Not:
    return Not(any_of(*conds))


def negate(cond: Condition | bool) -> Not:
    return Not(cast(Condition, cond))


def contains(field: str, text: str) -> Match:
    return Match(as_field(field), "contains", text)


def one_of[T](field: T, values: Iterable[T]) -> OneOf:
    return OneOf(as_field(field), tuple(values))


# Type checkers see `Order.total` as an `int`, so the functions that take a field are typed with its value type.
def as_field(value: object) -> FieldRef:
    """The field `value` stands for; a relation such as `Order.customer` stands for its `Ref` field."""
    if isinstance(value, Path):
        return value.__inlay_field__()
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
        case _:
            raise TypeError(f"expected a condition, got {cond!r}")


def check_condition(cls: type, cond: Condition) -> None:
    """Raise if `cond` tests a field that cannot be reached from `cls`."""
    for ref in fields(cond):
        start = ref.via[0][0] if ref.via else ref.owner
        if start is not cls:
            raise ValueError(f"{ref.owner.__name__}.{ref.name} cannot be used on {cls.__name__}")


def classes_read(cond: Condition) -> set[type]:
    """The classes whose tables are read to test `cond`: the one it applies to and those it reaches by relations."""
    return {cls for ref in fields(cond) for cls in (ref.owner, *(owner for owner, _ in ref.via))}
