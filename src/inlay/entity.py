import re
import uuid
from annotationlib import Format, get_annotations
from collections.abc import Callable, Sequence
from dataclasses import Field, dataclass, field, fields
from typing import Any, dataclass_transform, get_args, get_origin, overload

from inlay.conditions import FieldRef
from inlay.meta import EntityMeta, FieldMeta
from inlay.types import SQL_TYPES

_KEY = "inlay_key"
# Reading `Order.customer.startswith` gives the method, so a field with that name could not be reached.
_FIELD_REF_METHODS = {name for name in vars(FieldRef) if not name.startswith("_")}


def key() -> Any:
    return field(default_factory=lambda: str(uuid.uuid7()), metadata={_KEY: True})


class Ref[T]:
    """Marks a field as the key of a `T`.

    Only type checkers use this class: it makes `o.customer` a `str` and `Order.customer` a `type[Customer]`.
    At runtime `@entity` replaces it with its own descriptor.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> type[T]: ...
    @overload
    def __get__(self, obj: object, owner: type) -> str: ...
    def __get__(self, obj: object, owner: type) -> Any:
        raise NotImplementedError

    def __set__(self, obj: object, value: str) -> None:
        raise NotImplementedError


class _Field:
    """Wraps a slot: the value on objects, a `FieldRef` on the class."""

    def __init__(self, slot: Any, meta: FieldMeta) -> None:
        self._slot = slot
        self._meta = meta

    def __get__(self, obj: object, owner: type | None = None) -> Any:
        if obj is not None:
            return self._slot.__get__(obj, owner)
        return FieldRef((self._meta,))

    def __set__(self, obj: object, value: object) -> None:
        self._slot.__set__(obj, value)


@overload
def entity[C](cls: type[C], /) -> type[C]: ...
@overload
def entity[C](
    *, table: str | None = None, indexes: Sequence[tuple[str, ...]] = ()
) -> Callable[[type[C]], type[C]]: ...
@dataclass_transform(frozen_default=True, kw_only_default=True)
def entity(
    cls: type | None = None, /, *, table: str | None = None, indexes: Sequence[tuple[str, ...]] = ()
) -> Any:
    def wrap(c: type) -> type:
        return _make_entity(c, table or _snake_case(c.__name__), tuple(tuple(i) for i in indexes))

    return wrap if cls is None else wrap(cls)


def _make_entity(cls: type, table: str, indexes: tuple[tuple[str, ...], ...]) -> type:
    new = dataclass(frozen=True, slots=True, kw_only=True)(cls)
    m = _entity_meta(new, table, indexes)
    setattr(new, "__inlay__", m)
    for f in m.fields:
        setattr(new, f.name, _Field(new.__dict__[f.name], f))
    return new


def _entity_meta(cls: type, table: str, indexes: tuple[tuple[str, ...], ...]) -> EntityMeta:
    # FORWARDREF leaves names that do not exist yet, such as the class itself, as forward references.
    hints = get_annotations(cls, format=Format.FORWARDREF)
    field_metas = tuple(_field_meta(cls, f, hints[f.name]) for f in fields(cls))
    _check(cls.__name__, field_metas, indexes)
    by_name = {f.name: f for f in field_metas}
    return EntityMeta(cls, table, field_metas, tuple(tuple(by_name[name] for name in index) for index in indexes))


def _field_meta(owner: type, f: Field[Any], hint: Any) -> FieldMeta:
    is_key = bool(f.metadata.get(_KEY))
    if get_origin(hint) is Ref:
        (ref,) = get_args(hint)
        return FieldMeta(owner, f.name, f"{f.name}_id", str, is_key, ref)
    if hint not in SQL_TYPES:
        raise TypeError(f"{owner.__name__}.{f.name} has unsupported type {hint!r}")
    return FieldMeta(owner, f.name, f.name, hint, is_key)


def _check(cls_name: str, field_metas: tuple[FieldMeta, ...], indexes: tuple[tuple[str, ...], ...]) -> None:
    keys = [f for f in field_metas if f.is_key]
    if len(keys) != 1:
        raise TypeError(f"{cls_name} needs exactly one field declared with key(), found {len(keys)}")
    if keys[0].py_type is not str or keys[0].ref is not None:
        raise TypeError(f"{cls_name}.{keys[0].name} is the key and must be a str")

    if clashes := [f.name for f in field_metas if f.name in _FIELD_REF_METHODS]:
        raise TypeError(f"{cls_name} cannot have fields named {', '.join(clashes)}: field references use these names")

    columns = [f.column for f in field_metas]
    for column in columns:
        if columns.count(column) > 1:
            raise TypeError(f"{cls_name} has two fields stored in column {column!r}")

    for index in indexes:
        if not index:
            raise TypeError(f"{cls_name} has an empty index")
        for name in index:
            if name not in {f.name for f in field_metas}:
                raise TypeError(f"{cls_name} index {index} names unknown field {name!r}")

    leading = {index[0] for index in indexes}
    for f in field_metas:
        if f.ref is not None and f.name not in leading:
            raise TypeError(
                f"{cls_name}.{f.name} is a Ref but no index starts with it; add ({f.name!r},) to indexes"
            )


def _snake_case(name: str) -> str:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
