import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from inlay.entity import EntityMeta, meta
from inlay.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    LIST_TABLES,
    ROLLBACK,
    clear_sql,
    create_index_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    insert_sql,
    select_by_key_sql,
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


@dataclass(frozen=True, slots=True)
class Table[T]:
    _conn: sqlite3.Connection
    _meta: EntityMeta

    def get(self, key: str) -> T | None:
        row = self._conn.execute(select_by_key_sql(self._meta), (key,)).fetchone()
        return None if row is None else _from_row(self._meta, row)

    def delete(self, *keys: str) -> None:
        with _transaction(self._conn):
            deleted = self._conn.executemany(delete_by_key_sql(self._meta), [(k,) for k in keys]).rowcount
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self) -> None:
        with _transaction(self._conn):
            self._conn.execute(clear_sql(self._meta))


class Db:
    def __init__(self, conn: sqlite3.Connection, metas: Sequence[EntityMeta]) -> None:
        self._conn = conn
        self._metas = {m.cls: m for m in metas}

    def __getitem__[T](self, cls: type[T]) -> Table[T]:
        return Table(self._conn, self._meta(cls))

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
                    raise Missing(f"{len(group) - deleted} of the {m.cls.__name__} objects are not stored")

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


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Joins an open transaction, or runs a new one that commits at the end."""
    with _store_errors():
        if conn.in_transaction:
            yield
            return
        conn.execute(BEGIN)
        try:
            yield
            conn.execute(COMMIT)
        except BaseException:
            conn.execute(ROLLBACK)
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
    metas = tuple(map(meta, entities))
    _check_refs(metas)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _execute_all(conn, (*CONNECTION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        _check_tables(metas, tables)
        _execute_all(conn, (*_create_statements(metas, tables), COMMIT))
    except BaseException:
        conn.close()  # also rolls back the schema changes
        raise
    return Db(conn, metas)


def _execute_all(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        conn.execute(statement)


def _check_refs(metas: Sequence[EntityMeta]) -> None:
    classes = {m.cls for m in metas}
    dangling = [
        f"{m.cls.__name__}.{f.name} -> {f.target.__name__}"
        for m in metas
        for f in m.fields
        if f.target is not None and f.target not in classes
    ]
    if dangling:
        raise TypeError(f"Ref fields point to entities not passed to inlay.open: {', '.join(dangling)}")


def _check_tables(metas: Sequence[EntityMeta], tables: Mapping[str, str]) -> None:
    """Raise if a table already in the db was created differently from what its entity needs now."""
    changed = [m for m in metas if m.table in tables and tables[m.table] != create_table_sql(m)]
    if changed:
        raise SchemaMismatch("\n".join(_mismatch(m, tables[m.table]) for m in changed))


def _mismatch(m: EntityMeta, stored_sql: str) -> str:
    return f"table {m.table!r} does not match its entity:\n  in db:  {stored_sql}\n  entity: {create_table_sql(m)}"


def _create_statements(metas: Sequence[EntityMeta], tables: Mapping[str, str]) -> list[str]:
    """`CREATE TABLE` for each missing table, then `CREATE INDEX IF NOT EXISTS` for every index."""
    missing_tables = [create_table_sql(m) for m in metas if m.table not in tables]
    return missing_tables + [create_index_sql(m, index) for m in metas for index in m.indexes]
