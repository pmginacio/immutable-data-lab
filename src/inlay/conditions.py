from collections.abc import Iterable
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
        return Compare(self.__ref_field(), "==", other)

    def __ne__(self, other: object) -> Compare:  # type: ignore[override]
        return Compare(self.__ref_field(), "!=", other)

    def __ref_field(self) -> FieldRef:
        *via, (owner, name) = self.__via
        return FieldRef(owner, name, tuple(via))


def all_of(*conds: Condition | bool) -> And:
    return And(_conditions(conds))


def any_of(*conds: Condition | bool) -> Or:
    return Or(_conditions(conds))


def none_of(*conds: Condition | bool) -> Not:
    return Not(any_of(*conds))


def negate(cond: Condition | bool) -> Not:
    return Not(*_conditions((cond,)))


def contains(field: str, text: str) -> Match:
    return Match(as_field(field), "contains", text)


def one_of[T](field: T, values: Iterable[T]) -> OneOf:
    return OneOf(as_field(field), tuple(values))


# Type checkers see `Order.total` as an `int`, so the functions that take a field are typed with its value type.
def as_field(value: object) -> FieldRef:
    if not isinstance(value, FieldRef):
        raise TypeError(f"expected a field such as Order.total, got {value!r}")
    return value


def _conditions(conds: Iterable[Condition | bool]) -> tuple[Condition, ...]:
    return cast(tuple[Condition, ...], tuple(conds))
