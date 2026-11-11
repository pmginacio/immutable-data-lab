import os
import sqlite3
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Literal, Self, overload

from inlay.conditions import Condition, FieldRef, all_of, as_field
from inlay.entity import EntityMeta, meta
from inlay.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    LIST_TABLES,
    RELEASE,
    ROLLBACK,
    ROLLBACK_TO,
    SAVEPOINT,
    Query,
    aggregate_sql,
    clear_sql,
    create_index_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    insert_sql,
    select_sql,
    upsert_sql,
)
from inlay.types import from_sql, to_sql


class StoreError(Exception):
    pass


class SchemaMismatch(StoreError):
    pass


class AlreadyExists(StoreError):
    pass


class Missing(StoreError):
    pass


class BrokenRef(StoreError):
    pass


_MISSING: Final = object()


@dataclass(frozen=True, slots=True, eq=False)
class RecordView[T]:
    """A lazy, read-only collection of records. Every read runs its query again."""

    _conn: sqlite3.Connection
    _meta: EntityMeta
    _query: Query = Query()

    def __iter__(self) -> Iterator[T]:
        sql, params = select_sql(self._meta, self._query)
        for row in self._conn.execute(sql, params):
            yield _from_row(self._meta, row)

    def __len__(self) -> int:
        return self._aggregate("COUNT")

    def __contains__(self, record: object) -> bool:
        if type(record) is not self._meta.cls:
            return False
        equal = (getattr(self._meta.cls, f.name) == getattr(record, f.name) for f in self._meta.fields)
        return len(self.filter(*equal)) > 0

    def __getitem__(self, s: slice) -> "RecordView[T]":
        if not isinstance(s, slice):
            raise TypeError("a view can only be sliced, as in view[:10]")
        start, stop = s.start or 0, s.stop
        if s.step is not None or start < 0 or (stop is not None and stop < 0):
            raise ValueError("a slice takes a start and a stop, and no negative numbers")
        return self._view(self._query.sliced(start, stop))

    def filter(self, *conds: Condition | bool) -> "RecordView[T]":
        return self._view(self._query.filter(all_of(*conds).items))

    def sorted(self, *, key: Any, reverse: bool = False) -> "RecordView[T]":
        """`key` is a field, or a tuple of fields."""
        fields = key if isinstance(key, tuple) else (key,)
        return self._view(self._query.sorted(tuple(map(as_field, fields)), reverse))

    def get(self, key: str) -> T | None:
        key_field = FieldRef(self._meta.cls, self._meta.key.name, ())
        return next(iter(self.filter(key_field == key)), None)

    @overload
    def sum(self, field: int) -> int: ...
    @overload
    def sum(self, field: float) -> float: ...
    def sum(self, field: Any) -> Any:
        return self._aggregate("SUM", as_field(field)) or 0

    @overload
    def min[V](self, field: V) -> V: ...
    @overload
    def min[V, D](self, field: V, default: D) -> V | D: ...
    def min(self, field: Any, default: Any = _MISSING) -> Any:
        return self._extreme("MIN", field, default)

    @overload
    def max[V](self, field: V) -> V: ...
    @overload
    def max[V, D](self, field: V, default: D) -> V | D: ...
    def max(self, field: Any, default: Any = _MISSING) -> Any:
        return self._extreme("MAX", field, default)

    def _extreme(self, function: Literal["MIN", "MAX"], field: Any, default: object) -> Any:
        ref = as_field(field)
        value = self._aggregate(function, ref)
        if value is not None:
            return from_sql(value, self._meta.field(ref.name).py_type)
        if default is _MISSING:
            raise ValueError(f"{function.lower()}() of an empty view")
        return default

    def _view(self, query: Query) -> "RecordView[T]":
        return RecordView(self._conn, self._meta, query)

    def _aggregate(self, function: Literal["COUNT", "SUM", "MIN", "MAX"], field: FieldRef | None = None) -> Any:
        sql, params = aggregate_sql(self._meta, self._query, function, field)
        return self._conn.execute(sql, params).fetchone()[0]


@dataclass(frozen=True, slots=True, eq=False)
class RecordSet[T](RecordView[T]):
    """All the records of one class. Unlike a view, it can also delete."""

    def delete(self, *keys: str) -> None:
        with _transaction(self._conn):
            deleted = self._conn.executemany(delete_by_key_sql(self._meta), [(k,) for k in keys]).rowcount
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self) -> None:
        with _transaction(self._conn):
            self._conn.execute(clear_sql(self._meta))


class Db:
    def __init__(self, conn: sqlite3.Connection, metas: Mapping[type, EntityMeta]) -> None:
        self._conn = conn
        self._metas = metas

    def __getitem__[T](self, cls: type[T]) -> RecordSet[T]:
        return RecordSet(self._conn, self._meta(cls))

    def transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn)

    def add(self, *objs: object) -> None:
        self._write(insert_sql, objs)

    def update(self, *objs: object) -> None:
        self._write(upsert_sql, objs)

    def remove(self, *objs: object) -> None:
        with _transaction(self._conn):
            for m, group in self._grouped(objs):
                deleted = self._conn.executemany(delete_equal_sql(m), [_row(m, o) for o in group]).rowcount
                if deleted != len(group):
                    raise Missing(f"{len(group) - deleted} of the {m.cls.__name__} records are not stored")

    def discard(self, *objs: object) -> None:
        self._write(delete_equal_sql, objs)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _meta(self, cls: type) -> EntityMeta:
        try:
            return self._metas[cls]
        except KeyError:
            raise KeyError(f"{cls.__name__} was not passed to inlay.open") from None

    def _grouped(self, objs: Sequence[object]) -> list[tuple[EntityMeta, list[object]]]:
        classes = dict.fromkeys(type(o) for o in objs)
        return [(self._meta(cls), [o for o in objs if type(o) is cls]) for cls in classes]

    def _write(self, sql_for: Callable[[EntityMeta], str], objs: Sequence[object]) -> None:
        with _transaction(self._conn):
            for m, group in self._grouped(objs):
                self._conn.executemany(sql_for(m), [_row(m, o) for o in group])


# ROLLBACK TO leaves the savepoint open, so it is released as well.
_TOP_LEVEL = (BEGIN, COMMIT, (ROLLBACK,))
_NESTED = (SAVEPOINT, RELEASE, (ROLLBACK_TO, RELEASE))


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Runs a new transaction, or a savepoint inside an open one, so a failed call leaves nothing behind."""
    begin, end, undo = _NESTED if conn.in_transaction else _TOP_LEVEL
    with _store_errors():
        conn.execute(begin)
        try:
            yield
            conn.execute(end)
        except BaseException:
            _execute_all(conn, undo)
            raise


@contextmanager
def _store_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.IntegrityError as e:
        match e.sqlite_errorname:
            case "SQLITE_CONSTRAINT_PRIMARYKEY":
                raise AlreadyExists(str(e)) from e
            case "SQLITE_CONSTRAINT_FOREIGNKEY":
                raise BrokenRef("a Ref would point to an object that is not stored") from e
        raise


def _row(m: EntityMeta, obj: object) -> tuple[object, ...]:
    return tuple(to_sql(getattr(obj, f.name)) for f in m.fields)


def _from_row(m: EntityMeta, row: Sequence[object]) -> Any:
    return m.cls(**{f.name: from_sql(value, f.py_type) for f, value in zip(m.fields, row, strict=True)})


def open(path: str | os.PathLike[str], entities: Iterable[type]) -> Db:
    metas = {m.cls: m for m in map(meta, entities)}
    _check_refs(metas)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _execute_all(conn, (*CONNECTION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        _check_tables(metas.values(), tables)
        create_tables = [create_table_sql(m) for m in metas.values() if m.table not in tables]
        create_indexes = [create_index_sql(m, i) for m in metas.values() for i in m.indexes]
        _execute_all(conn, (*create_tables, *create_indexes, COMMIT))
    except BaseException:
        conn.close()  # also rolls back the schema changes
        raise
    return Db(conn, metas)


def _execute_all(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def _check_refs(metas: Mapping[type, EntityMeta]) -> None:
    dangling = [
        f"{m.cls.__name__}.{f.name} -> {f.target.__name__}"
        for m in metas.values()
        for f in m.fields
        if f.target is not None and f.target not in metas
    ]
    if dangling:
        raise TypeError(f"Ref fields point to entities not passed to inlay.open: {', '.join(dangling)}")


def _check_tables(metas: Collection[EntityMeta], tables: Mapping[str, str]) -> None:
    """Raise if a table already in the db was created differently from what its entity needs now."""
    changed = [m for m in metas if m.table in tables and tables[m.table] != create_table_sql(m)]
    if changed:
        raise SchemaMismatch("\n".join(_mismatch(m, tables[m.table]) for m in changed))


def _mismatch(m: EntityMeta, stored_sql: str) -> str:
    return f"table {m.table!r} does not match its entity:\n  in db:  {stored_sql}\n  entity: {create_table_sql(m)}"

