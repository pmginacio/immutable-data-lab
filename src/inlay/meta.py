import re
from annotationlib import ForwardRef
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldMeta:
    owner: type
    name: str
    py_type: type
    is_key: bool = False
    # What a `Ref` field points to, as written; a forward reference until that class exists.
    ref: type | ForwardRef | None = None

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


def table_name(class_name: str) -> str:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
