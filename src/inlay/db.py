import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self, overload
from urllib.parse import quote

from inlay.conditions import own_value
from inlay.functions import Aggregate, aggregate_of
from inlay.meta import EntityMeta, meta, table_name
from inlay.nodes import (
    Compare,
    Condition,
    Filter,
    Query,
    Slice,
    Sort,
    Table,
    Value,
    all_of,
    check_condition,
    classes_read,
    describe,
    record_class,
    result_type,
)
from inlay.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    FOREIGN_KEY_CHECK,
    LIST_INDEXES,
    LIST_TABLES,
    MIGRATION_SETUP,
    REFERRERS,
    RELEASE,
    ROLLBACK,
    ROLLBACK_TO,
    SAVEPOINT,
    aggregate_sql,
    clear_sql,
    create_index_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    drop_table_sql,
    insert_sql,
    rename_table_sql,
    repoint_sql,
    reprefix_index_sql,
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


@dataclass(frozen=True, eq=False)
class RecordView[T]:
    """A lazy, read-only collection of records. Every read runs its query again."""

    _conn: sqlite3.Connection
    _reading: Counter[type]
    _query: Query

    @property
    def _meta(self) -> EntityMeta:
        return meta(record_class(self._query))

    def __iter__(self) -> Iterator[T]:
        sql, params = select_sql(self._query)
        classes = classes_read(self._query)
        # SQLite does not say what an open SELECT returns once its tables change, so writes to them are refused.
        self._reading.update(classes)
        try:
            for row in self._conn.execute(sql, params):
                yield _from_row(self._meta, row)
        finally:
            self._reading.subtract(classes)

    def __len__(self) -> int:
        return self._aggregate(aggregate_of(len))

    def __contains__(self, record: object) -> bool:
        if type(record) is not self._meta.cls:
            return False
        equal = (Compare(f, "==", getattr(record, f.name)) for f in self._meta.fields)
        return len(self.filter(*equal)) > 0

    def __getitem__(self, s: slice) -> "RecordView[T]":
        if not isinstance(s, slice):
            raise TypeError("a view can only be sliced, as in view[:10]")
        start, stop = s.start or 0, s.stop
        if s.step is not None or start < 0 or (stop is not None and stop < 0):
            raise ValueError("a slice takes a start and a stop, and no negative numbers")
        return self._view(Slice(self._query, start, stop))

    def filter(self, *conds: Condition | bool) -> "RecordView[T]":
        cond = all_of(*conds)
        check_condition(self._meta.cls, cond)
        return self._view(Filter(self._query, cond))

    def sorted(self, *, key: Any, reverse: bool = False) -> "RecordView[T]":
        """`key` is a value such as `Order.total` or `Order.total * 2`, or a tuple of values."""
        keys = key if isinstance(key, tuple) else (key,)
        return self._view(Sort(self._query, tuple(own_value(self._meta.cls, k) for k in keys), reverse))

    def get(self, key: str) -> T | None:
        return next(iter(self.filter(Compare(self._meta.key, "==", key))), None)

    @overload
    def sum(self, value: int) -> int: ...
    @overload
    def sum(self, value: float) -> float: ...
    def sum(self, value: Any) -> Any:
        aggregate = aggregate_of(sum)
        return self._aggregate(aggregate, self._aggregated(aggregate, value)) or 0

    @overload
    def min[V](self, value: V) -> V: ...
    @overload
    def min[V, D](self, value: V, default: D) -> V | D: ...
    def min(self, value: Any, default: Any = _MISSING) -> Any:
        return self._extreme(min, value, default)

    @overload
    def max[V](self, value: V) -> V: ...
    @overload
    def max[V, D](self, value: V, default: D) -> V | D: ...
    def max(self, value: Any, default: Any = _MISSING) -> Any:
        return self._extreme(max, value, default)

    def aggregate_by[K, R](self, key: K, func: Callable[..., R], value: Any = None) -> dict[K, R]:
        """`func` of `value` over the records with each value of `key`, in key order; keys with no records are left out.

        `key` is a value or a tuple of values, as in `sorted`.
        `func` is `len`, which takes no `value`, `sum`, `min`, `max`, or an `Aggregate` such as `avg`.
        """
        aggregate = aggregate_of(func)
        if aggregate.arg is not None and value is None:
            raise TypeError(f"{aggregate.name} needs a value")
        many = isinstance(key, tuple)
        keys = tuple(own_value(self._meta.cls, k) for k in (key if many else (key,)))
        own = None if value is None else self._aggregated(aggregate, value)
        sql, params = aggregate_sql(self._query, aggregate, own, keys)
        rows = self._conn.execute(sql, params).fetchall()
        key_types = [result_type(k) for k in keys]
        value_type = aggregate.result or result_type(own)
        groups: dict[Any, Any] = {
            tuple(map(from_sql, row[:-1], key_types)): from_sql(row[-1], value_type) for row in rows
        }
        return groups if many else {k: v for (k,), v in groups.items()}

    def _aggregated(self, aggregate: Aggregate[Any], value: object) -> Value:
        """`value` as a value of the records of this view that `aggregate` can take."""
        own = own_value(self._meta.cls, value)
        if aggregate.arg is None:
            raise TypeError(f"{aggregate.name} takes no value")
        if not issubclass(t := result_type(own), aggregate.arg):
            raise TypeError(f"{aggregate.name} cannot take {describe(own)}, a {t.__name__}")
        return own

    def _extreme(self, func: Callable[..., Any], value: Any, default: object) -> Any:
        aggregate = aggregate_of(func)
        own = self._aggregated(aggregate, value)
        result = self._aggregate(aggregate, own)
        if result is not None:
            return from_sql(result, result_type(own))
        if default is _MISSING:
            raise ValueError(f"{aggregate.name}() of an empty view")
        return default

    def _view(self, query: Query) -> "RecordView[T]":
        return RecordView(self._conn, self._reading, query)

    def _aggregate(self, aggregate: Aggregate[Any], value: Value | None = None) -> Any:
        sql, params = aggregate_sql(self._query, aggregate, value)
        return self._conn.execute(sql, params).fetchone()[0]


class RecordSet[T](RecordView[T]):
    """All the records of one class. Unlike a view, it can also delete."""

    def delete(self, *keys: str) -> None:
        _check_idle(self._reading, [self._meta.cls])
        with _transaction(self._conn):
            deleted = self._conn.executemany(delete_by_key_sql(self._meta), ((k,) for k in keys)).rowcount
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self, *conds: Condition | bool) -> None:
        cond = all_of(*conds)
        check_condition(self._meta.cls, cond)
        sql, params = clear_sql(self._meta, cond)
        _check_idle(self._reading, [self._meta.cls])
        with _transaction(self._conn):
            self._conn.execute(sql, params)


class Db:
    def __init__(self, conn: sqlite3.Connection, metas: frozendict[type, EntityMeta]) -> None:
        self._conn = conn
        self._metas = metas
        self._reading: Counter[type] = Counter()

    def __getitem__[T](self, cls: type[T]) -> RecordSet[T]:
        return RecordSet(self._conn, self._reading, Table(self._meta(cls).cls))

    def transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn)

    def add(self, *objs: object) -> None:
        self._write(insert_sql, objs)

    def update(self, *objs: object) -> None:
        self._write(upsert_sql, objs)

    def remove(self, *objs: object) -> None:
        groups = _grouped(objs)
        _check_idle(self._reading, groups)
        with _transaction(self._conn):
            for cls, group in groups.items():
                m = self._meta(cls)
                deleted = self._conn.executemany(delete_equal_sql(m), (_row(m, o) for o in group)).rowcount
                if deleted != len(group):
                    raise Missing(f"{len(group) - deleted} of the {cls.__name__} records are not stored")

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

    def _write(self, sql_for: Callable[[EntityMeta], str], objs: Sequence[object]) -> None:
        groups = _grouped(objs)
        _check_idle(self._reading, groups)
        with _transaction(self._conn):
            for cls, group in groups.items():
                m = self._meta(cls)
                self._conn.executemany(sql_for(m), (_row(m, o) for o in group))


def _grouped(objs: Iterable[object]) -> dict[type, list[object]]:
    """The records of each class, so each class is written with one statement, in the order classes appear."""
    groups: defaultdict[type, list[object]] = defaultdict(list)
    for o in objs:
        groups[type(o)].append(o)
    return groups


def _check_idle(reading: Counter[type], classes: Iterable[type]) -> None:
    """Raise if an iteration that is still open reads the table of one of `classes`."""
    for cls in classes:
        if reading[cls] > 0:
            raise RuntimeError(f"{cls.__name__} changed during iteration")


# ROLLBACK TO leaves the savepoint open, so it is released as well.
_TOP_LEVEL = (BEGIN, COMMIT, ROLLBACK)
_NESTED = (SAVEPOINT, RELEASE, (ROLLBACK_TO, RELEASE))


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Runs a new transaction, or a savepoint inside an open one, so a failed call leaves nothing behind."""
    begin, end, undo = _NESTED if conn.in_transaction else _TOP_LEVEL
    with _store_errors():
        _execute(conn, begin)
        try:
            yield
            _execute(conn, end)
        except BaseException:
            _execute(conn, undo)
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
                raise BrokenRef("a Ref would point to a record that is not stored") from e
        raise


def _row(m: EntityMeta, obj: object) -> tuple[object, ...]:
    return tuple(to_sql(getattr(obj, f.name)) for f in m.fields)


def _from_row(m: EntityMeta, row: Sequence[object]) -> Any:
    return m.cls(**{f.name: from_sql(value, f.py_type) for f, value in zip(m.fields, row, strict=True)})


# Relative to the current directory, like any relative path.
DEFAULT_PATH: Final = ".inlay.db"


def open(entities: Iterable[type], *, path: str | os.PathLike[str] = DEFAULT_PATH) -> Db:
    metas = _metas(entities)
    _check_refs(metas)
    conn = _connect(path, create=True)
    try:
        _execute(conn, (*CONNECTION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        _check_tables(metas.values(), tables)
        create_tables = [create_table_sql(m) for m in metas.values() if m.table not in tables]
        create_indexes = [create_index_sql(m, i) for m in metas.values() for i in m.indexes]
        _execute(conn, (*create_tables, *create_indexes, COMMIT))
    except BaseException:
        conn.close()  # also rolls back the schema changes
        raise
    return Db(conn, metas)


def rename(old: str, new: str, *, path: str | os.PathLike[str] = DEFAULT_PATH) -> None:
    """Rename the table of the class called `old` to the one of the class called `new`, keeping its data.

    The references to the table in the other tables and the names of its indexes follow the new name.
    """
    old_table, new_table = table_name(old), table_name(new)
    conn = _connect(path, create=False)
    try:
        # With foreign keys on, SQLite also rewrites the references to the renamed table in the other tables.
        _execute(conn, ("PRAGMA foreign_keys = ON", BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        if old_table not in tables:
            raise SchemaMismatch(f"there is no table {old_table!r} to rename")
        if new_table in tables:
            raise SchemaMismatch(f"table {new_table!r} already exists")
        conn.execute(rename_table_sql(old_table, new_table))
        for name, sql in conn.execute(LIST_INDEXES, (new_table,)).fetchall():
            if name.startswith(f"{old_table}__"):
                _execute(conn, reprefix_index_sql(name, sql, old_table, new_table))
        conn.execute(COMMIT)
    finally:
        conn.close()  # also rolls back what was not committed


def migrate(
    fn: Callable[[Db], None],
    old: type,
    new: type,
    other_entities: Iterable[type] = (),
    *,
    path: str | os.PathLike[str] = DEFAULT_PATH,
) -> None:
    """Change the schema of one class and convert its records with `fn`, all or nothing.

    `old` describes the table as it is stored and `new` the table it becomes; the old table is dropped at the end.
    Either `new` takes the table name of the stored table (it is moved aside to the table name of `old`),
    or `new` has a table name of its own. `fn` gets a db with `old`, `new` and `other_entities`.
    Nothing happens if the db already has the table of `new`.
    """
    old_meta, new_meta = meta(old), meta(new)
    if old_meta.table == new_meta.table:
        raise ValueError(f"{old.__name__} and {new.__name__} use the same table; give the old class another name")
    metas = _metas((old, new, *other_entities))
    conn = _connect(path, create=False)
    try:
        _execute(conn, (*MIGRATION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        stored = tables.get(new_meta.table)
        if stored == create_table_sql(new_meta):
            return
        if stored == create_table_sql(old_meta, new_meta.table):
            # The stored text keeps its references to itself under the new table's name, so it is not checked again.
            conn.execute(rename_table_sql(new_meta.table, old_meta.table))
        elif tables.get(old_meta.table) != create_table_sql(old_meta) or stored is not None:
            raise SchemaMismatch(
                f"cannot migrate {old.__name__} to {new.__name__}: the db needs a table {old_meta.table!r} "
                f"that matches {old.__name__} and no table {new_meta.table!r}"
            )
        conn.execute(create_table_sql(new_meta))
        fn(Db(conn, metas))
        _repoint_referrers(conn, old_meta, new_meta)
        conn.execute(drop_table_sql(old_meta.table))
        # The indexes come last: a moved table keeps its index names, and `IF NOT EXISTS` would skip a clash.
        _execute(conn, (create_index_sql(new_meta, index) for index in new_meta.indexes))
        _check_tables(metas.values(), dict(conn.execute(LIST_TABLES)))
        _check_foreign_keys(conn)
        conn.execute(COMMIT)
    finally:
        conn.close()  # also rolls back what was not committed


def _repoint_referrers(conn: sqlite3.Connection, old: EntityMeta, new: EntityMeta) -> None:
    """Make the foreign keys of the other tables that point to the old table point to the new one."""
    tables = dict(conn.execute(LIST_TABLES))
    for (table,) in conn.execute(REFERRERS, (old.table,)).fetchall():
        if table != old.table:
            indexes = conn.execute(LIST_INDEXES, (table,)).fetchall()
            _execute(conn, repoint_sql(table, tables[table], old, new))
            _execute(conn, (sql for _, sql in indexes))  # dropping the table dropped them


def _check_foreign_keys(conn: sqlite3.Connection) -> None:
    broken = conn.execute(FOREIGN_KEY_CHECK).fetchall()
    if broken:
        table, _, parent, _ = broken[0]
        raise BrokenRef(
            f"{len(broken)} references point to records that are not stored, such as one in {table!r} to {parent!r}"
        )


def _connect(path: str | os.PathLike[str], *, create: bool) -> sqlite3.Connection:
    if create:
        return sqlite3.connect(path, autocommit=True)
    # Read-write mode only, so a wrong path raises instead of creating an empty file.
    return sqlite3.connect(f"file:{quote(os.fspath(path))}?mode=rw", uri=True, autocommit=True)


def _execute(conn: sqlite3.Connection, statements: str | Iterable[str]) -> None:
    """Runs one statement, or several in order."""
    # A string is also an iterable of strings, one for each character, so it is checked first.
    for statement in (statements,) if isinstance(statements, str) else statements:
        conn.execute(statement)


def _metas(entities: Iterable[type]) -> frozendict[type, EntityMeta]:
    # pyright's stub of frozendict declares an `__init__` with no arguments, although the constructor takes a mapping.
    return frozendict({m.cls: m for m in map(meta, entities)})  # pyright: ignore[reportCallIssue]


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

