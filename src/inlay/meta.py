import re
from annotationlib import ForwardRef
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldMeta:
    owner: type
    name: str
    py_type: type
    is_key: bool = False
    # What a `Ref` field points to, as written; a forward reference until that class exists.
    ref: type | ForwardRef | None = None
    optional: bool = False

    @property
    def target(self) -> type | None:
        """The class a `Ref` field points to, or None for other fields."""
        return self.ref.evaluate() if isinstance(self.ref, ForwardRef) else self.ref


@dataclass(frozen=True)
class EntityMeta:
    cls: type
    table: str
    fields: tuple[FieldMeta, ...]
    indexes: tuple[tuple[FieldMeta, ...], ...]

    @property
    def key(self) -> FieldMeta:
        return next(f for f in self.fields if f.is_key)

    def field(self, name: str) -> FieldMeta:
        found = next((f for f in self.fields if f.name == name), None)
        if found is None:
            raise AttributeError(f"{self.cls.__name__} has no field {name!r}")
        return found


def meta(cls: type) -> EntityMeta:
    m = cls.__dict__.get("__inlay__")
    if not isinstance(m, EntityMeta):
        raise TypeError(f"{cls.__name__} is not an @entity class")
    return m


def metas(entities: Iterable[type]) -> frozendict[type, EntityMeta]:
    # pyright's stub of frozendict declares an `__init__` with no arguments, although the constructor takes a mapping.
    found = frozendict({m.cls: m for m in map(meta, entities)})  # pyright: ignore[reportCallIssue]
    owners: dict[str, type] = {}
    for m in found.values():
        owner = owners.setdefault(m.table, m.cls)
        if owner is not m.cls:
            raise TypeError(f"{owner.__qualname__} and {m.cls.__qualname__} both use the table {m.table!r}")
    return found


def reachable(cls: type) -> list[EntityMeta]:
    """The meta of `cls` and of every class its `Ref`s reach, through other classes too, each once."""
    found: dict[type, EntityMeta] = {}
    todo = [cls]
    while todo:
        c = todo.pop()
        if c not in found:
            m = found[c] = meta(c)
            todo.extend(f.target for f in m.fields if f.target is not None)
    return list(found.values())


def table_name(class_name: str) -> str:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
