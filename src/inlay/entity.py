import hashlib
import json
import uuid
from annotationlib import Format, get_annotations
from collections.abc import Callable, Sequence
import dataclasses
from dataclasses import dataclass, fields
from types import NoneType, UnionType
from typing import Any, dataclass_transform, get_args, get_origin, overload

from inlay.conditions import Expr, Field, own_value
from inlay.meta import EntityMeta, FieldMeta, meta, table_name
from inlay.nodes import BinOp, Call, GroupValue, Projection, RefValue, Value, result_type
from inlay.types import sql_type, to_sql

_KEY = "inlay_key"
_HASHED = "inlay_hashed"
# Reading `Order.customer.startswith` gives the method, so a field with that name could not be reached.
_EXPR_METHODS = {name for name in vars(Expr) if not name.startswith("__")}


def key() -> Any:
    return dataclasses.field(default_factory=lambda: str(uuid.uuid7()), metadata={_KEY: True})


def default(value: Any) -> Any:
    """The default of a field, as `discount: Field[int | None] = default(None)`.

    A plain `= None` would be checked against `Field[...]`, which type checkers refuse. Typed as returning
    anything, as `key()` is, it passes, so checkers do not check the value against the field's type.
    """
    return dataclasses.field(default=value)


def hashed() -> Any:
    """A key computed from the other fields when the record is built, so records with the same values have the same key."""
    return dataclasses.field(init=False, metadata={_KEY: True, _HASHED: True})


def restored(cls: type, values: dict[str, object]) -> Any:
    """A record of `cls` with `values`, as read back from the db.

    It is set field by field, without `__init__` or `__post_init__`, so a hashed key stays as stored.
    """
    record = object.__new__(cls)
    for name, value in values.items():
        # The class is frozen, and setting through `object` is how a frozen dataclass sets its own fields.
        object.__setattr__(record, name, value)
    return record


class Ref[T, V = str]:
    """Marks a field as the key of a `T`, or with `Ref[T, str | None]` as an optional one.

    Only type checkers use this class: it makes `o.customer` a `V` and `Order.customer` a `type[Customer]`.
    At runtime `@entity` replaces it with its own descriptor.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> type[T]: ...
    @overload
    def __get__(self, obj: object, owner: type) -> V: ...
    def __get__(self, obj: object, owner: type) -> Any:
        raise NotImplementedError

    def __set__(self, obj: object, value: V) -> None:
        raise NotImplementedError


@overload
def entity[C](cls: type[C], /) -> type[C]: ...
@overload
def entity[C](*, indexes: Sequence[tuple[str, ...]] = ()) -> Callable[[type[C]], type[C]]: ...
@dataclass_transform(frozen_default=True, kw_only_default=True)
def entity(cls: type | None = None, /, *, indexes: Sequence[tuple[str, ...]] = ()) -> Any:
    def wrap(c: type) -> type:
        return _make_entity(c, tuple(tuple(i) for i in indexes))

    return wrap if cls is None else wrap(cls)


def _make_entity(cls: type, indexes: tuple[tuple[str, ...], ...]) -> type:
    if any(isinstance(v, dataclasses.Field) and v.metadata.get(_HASHED) for v in vars(cls).values()):
        _hash_key_on_init(cls)
    new = dataclass(frozen=True, kw_only=True)(cls)
    m = _entity_meta(new, indexes)
    return _with_fields(new, "__inlay__", m, m)


def _hash_key_on_init(cls: type) -> None:
    """Gives `cls` a `__post_init__` that computes the key, before running the class's own.

    It is set before `dataclass` runs, since the `__init__` that `dataclass` writes calls only a `__post_init__` it finds.
    """
    own = vars(cls).get("__post_init__")

    def __post_init__(self: object) -> None:
        m = meta(type(self))
        object.__setattr__(self, m.key.name, _content_hash(m, self))
        if own is not None:
            own(self)

    setattr(cls, "__post_init__", __post_init__)


def _content_hash(m: EntityMeta, record: object) -> str:
    """The SHA-256 of the stored values of the fields other than the key, such as a datetime in UTC."""
    values = [[f.name, to_sql(getattr(record, f.name))] for f in m.fields if not f.is_key]
    text = json.dumps([[name, v.hex() if isinstance(v, bytes) else v] for name, v in values], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass_transform(frozen_default=True, kw_only_default=True)
def projection[C](root: type) -> Callable[[type[C]], type[C]]:
    """Makes a class a read-only shape of the records of `root`, read with `view.project(cls)`.

    A field named as a field of `root` takes its value; any other field is given its value, such as
    `city: str = Order.customer.city`. One field must take the key of `root`.
    """
    root_meta = meta(root)

    def wrap(cls: type) -> type:
        values = _take_values(cls)
        new = dataclass(frozen=True, kw_only=True)(cls)
        p = _projection(new, root_meta, values)
        return _with_fields(new, "__inlay_projection__", p, p.m)

    return wrap


def projection_of(cls: type) -> Projection:
    p = cls.__dict__.get("__inlay_projection__")
    if not isinstance(p, Projection):
        raise TypeError(f"{cls.__name__} is not a @projection class")
    return p


def _take_values(cls: type) -> dict[str, Expr]:
    """Removes the values written in the class, such as `city: str = Order.customer.city`, and returns them by field.

    `dataclass` would take them as defaults. Type checkers still do, so they accept a row built without them.
    """
    values = {name: v for name, v in vars(cls).items() if isinstance(v, Expr)}
    for name in values:
        delattr(cls, name)
    return values


def _with_fields(cls: type, attribute: str, description: object, m: EntityMeta) -> type:
    """`cls` with `description` stored as `attribute`, and each field of `m` an `Expr` on the class."""
    setattr(cls, attribute, description)
    for f in m.fields:
        setattr(cls, f.name, Field(f))
    return cls


def _projection(cls: type, root: EntityMeta, values: dict[str, Expr]) -> Projection:
    hints = get_annotations(cls)
    found = [_projected(cls, root, f.name, hints[f.name], values) for f in fields(cls)]
    field_metas = tuple(fm for fm, _ in found)
    if not any(fm.is_key for fm in field_metas):
        raise TypeError(f"{cls.__name__} needs a field with the key of {root.cls.__name__}, {root.key.name}")
    _check_names(cls.__name__, field_metas)
    m = EntityMeta(cls, "", field_metas, ())  # no table: its rows come from a SELECT of the root
    return Projection(root.cls, m, tuple(v for _, v in found))


def _projected(owner: type, root: EntityMeta, name: str, hint: Any, values: dict[str, Expr]) -> tuple[FieldMeta, Value]:
    """The field `name` of the projection `owner`, and its value on a record of `root`."""
    if name in values:
        value = own_value(root.cls, values[name])
    elif name in {f.name for f in root.fields}:
        value = root.field(name)
    else:
        raise TypeError(f"{owner.__name__}.{name} is not a field of {root.cls.__name__}, so it needs a value")
    declared = _declared(owner, name, hint)
    py_type, optional = _without_none(declared)
    if (py_type, optional) != (result_type(value), _may_be_none(value)):
        expected = result_type(value).__name__ + (" | None" if _may_be_none(value) else "")
        shown = declared.__name__ if isinstance(declared, type) else repr(declared)
        raise TypeError(f"{owner.__name__}.{name} is declared Field[{shown}], but its value is a {expected}")
    return FieldMeta(owner, name, py_type, value == root.key, None, optional), value


def _may_be_none(value: object) -> bool:
    """Whether `value` can be None on a record: an optional field, or a value read through an optional `Ref`."""
    match value:
        case FieldMeta():
            return value.optional
        case RefValue(refs, inner):
            return any(r.optional for r in refs) or _may_be_none(inner)
        case Call(fn, args):
            may = [v is None or _may_be_none(v) for v in args]
            return all(may) if fn.takes_none() else any(may)
        case BinOp(_, left, right):
            return _may_be_none(left) or _may_be_none(right)
        case GroupValue():
            return True  # a key with no records
        case _:
            return False  # a number in arithmetic


def _entity_meta(cls: type, indexes: tuple[tuple[str, ...], ...]) -> EntityMeta:
    # FORWARDREF leaves names that do not exist yet, such as the class itself, as forward references.
    hints = get_annotations(cls, format=Format.FORWARDREF)
    field_metas = tuple(_field_meta(cls, f, hints[f.name]) for f in fields(cls))
    _check(cls.__name__, field_metas, indexes)
    by_name = {f.name: f for f in field_metas}
    return EntityMeta(cls, table_name(cls.__name__), field_metas,tuple(tuple(by_name[name] for name in index) for index in indexes))


def _field_meta(owner: type, f: dataclasses.Field[Any], hint: Any) -> FieldMeta:
    is_key = bool(f.metadata.get(_KEY))
    ref, value = get_args(hint) if get_origin(hint) is Ref else (None, _declared(owner, f.name, hint))
    value, optional = _without_none(value)
    if sql_type(value) is None or (ref is not None and value is not str):
        raise TypeError(f"{owner.__name__}.{f.name} has unsupported type {hint!r}")
    return FieldMeta(owner, f.name, value, is_key, ref, optional)


def _declared(owner: type, name: str, hint: Any) -> Any:
    """The type of the field `name` of `owner`, declared as `Field[int]`."""
    if get_origin(hint) is not Field:
        raise TypeError(f"{owner.__name__}.{name} is declared {hint!r}; declare it as Field[...], or Ref[...] for a reference")
    return get_args(hint)[0]


def _without_none(hint: Any) -> tuple[Any, bool]:
    """`hint` without its `| None`, and whether it had one; a union of other types stays as it is."""
    if not (isinstance(hint, UnionType) and NoneType in get_args(hint)):
        return hint, False
    others = [arg for arg in get_args(hint) if arg is not NoneType]
    return (others[0] if len(others) == 1 else hint), True


def _check(cls_name: str, field_metas: tuple[FieldMeta, ...], indexes: tuple[tuple[str, ...], ...]) -> None:
    keys = [f for f in field_metas if f.is_key]
    if len(keys) != 1:
        raise TypeError(f"{cls_name} needs exactly one field declared with key(), found {len(keys)}")
    if keys[0].py_type is not str or keys[0].ref is not None or keys[0].optional:
        raise TypeError(f"{cls_name}.{keys[0].name} is the key and must be a str")

    _check_names(cls_name, field_metas)

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


def _check_names(cls_name: str, field_metas: tuple[FieldMeta, ...]) -> None:
    if clashes := [f.name for f in field_metas if f.name in _EXPR_METHODS]:
        raise TypeError(f"{cls_name} cannot have fields named {', '.join(clashes)}: field references use these names")
