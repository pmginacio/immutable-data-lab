import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Self, overload

from inlay.conditions import own_value
from inlay.db import AlreadyExists, BrokenRef, Missing, SchemaMismatch, StoreError
from inlay.functions import Aggregate, aggregate_of
from inlay.history.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    CREATE_TX_TABLE,
    CURRENT,
    LATEST_TX,
    LIST_TABLES,
    NEXT_TX,
    RELEASE,
    ROLLBACK,
    ROLLBACK_TO,
    SAVEPOINT,
    TX_AT,
    TX_TABLE,
    aggregate_sql,
    clear_sql,
    create_index_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    insert_sql,
    select_sql,
    version_index_sqls,
)
from inlay.meta import EntityMeta, meta
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
from inlay.types import from_sql, to_sql

_MISSING: Final = object()


@dataclass(frozen=True, eq=False)
class RecordView[T]:
    """A lazy, read-only collection of records. Every read runs its query again.

    It reads the records as they were at transaction `_tx`, or the current ones if it is None.
    """

    _conn: sqlite3.Connection
    _reading: Counter[type]
    _query: Query
    _tx: int | None = None

    @property
    def _meta(self) -> EntityMeta:
        return meta(record_class(self._query))

    def __iter__(self) -> Iterator[T]:
        sql, params = select_sql(self._query, self._tx)
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
        sql, params = aggregate_sql(self._query, aggregate, own, keys, self._tx)
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
        return RecordView(self._conn, self._reading, query, self._tx)

    def _aggregate(self, aggregate: Aggregate[Any], value: Value | None = None) -> Any:
        sql, params = aggregate_sql(self._query, aggregate, value, tx=self._tx)
        return self._conn.execute(sql, params).fetchone()[0]


class RecordSet[T](RecordView[T]):
    """All the records of one class. Unlike a view, it can also delete."""

    def delete(self, *keys: str) -> None:
        _check_idle(self._reading, [self._meta.cls])
        with _transaction(self._conn):
            deleted = self._conn.executemany(delete_by_key_sql(self._meta), [(k,) for k in keys]).rowcount
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self, *conds: Condition | bool) -> None:
        cond = all_of(*conds)
        check_condition(self._meta.cls, cond)
        sql, params = clear_sql(self._meta, cond)
        _check_idle(self._reading, [self._meta.cls])
        with _transaction(self._conn):
            self._conn.execute(sql, params)


@dataclass(frozen=True, eq=False)
class Snapshot:
    """The db as it was at transaction `tx`: the same reads as the db, and no writes."""

    _conn: sqlite3.Connection
    _reading: Counter[type]  # shared with the db, which writes on the same connection
    _metas: Mapping[type, EntityMeta]
    tx: int

    def __getitem__[T](self, cls: type[T]) -> RecordView[T]:
        return RecordView(self._conn, self._reading, Table(_meta_in(self._metas, cls).cls), self.tx)


class Db:
    def __init__(self, conn: sqlite3.Connection, metas: Mapping[type, EntityMeta]) -> None:
        self._conn = conn
        self._metas = metas
        self._reading: Counter[type] = Counter()

    def __getitem__[T](self, cls: type[T]) -> RecordSet[T]:
        return RecordSet(self._conn, self._reading, Table(_meta_in(self._metas, cls).cls))

    @property
    def tx(self) -> int:
        """The number of the latest transaction, or 0 before the first write."""
        return self._conn.execute(LATEST_TX).fetchone()[0]

    def as_of(self, at: int | datetime) -> Snapshot:
        """The db as it was at transaction `at`, or at the last transaction at or before the time `at`."""
        if isinstance(at, datetime):
            if at.tzinfo is None:
                raise ValueError("as_of needs a datetime with a timezone, because transaction times are in UTC")
            tx = self._conn.execute(TX_AT, (to_sql(at),)).fetchone()[0]
        elif not 0 <= at <= self.tx:
            raise ValueError(f"there is no transaction {at}; the latest is {self.tx}")
        else:
            tx = at
        return Snapshot(self._conn, self._reading, self._metas, tx)

    def transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn)

    def add(self, *objs: object) -> None:
        self._write(insert_sql, objs)

    def update(self, *objs: object) -> None:
        """Ends the current version with each record's key, if there is one, and adds the record as the new one."""
        groups = self._grouped(objs)
        _check_idle(self._reading, [m.cls for m, _ in groups])
        with _transaction(self._conn):
            for m, group in groups:
                # One record at a time, so the same key twice in a call ends with the last record.
                for o in group:
                    self._conn.execute(delete_by_key_sql(m), (to_sql(getattr(o, m.key.name)),))
                    self._conn.execute(insert_sql(m), _row(m, o))

    def remove(self, *objs: object) -> None:
        groups = self._grouped(objs)
        _check_idle(self._reading, [m.cls for m, _ in groups])
        with _transaction(self._conn):
            for m, group in groups:
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

    def _grouped(self, objs: Sequence[object]) -> list[tuple[EntityMeta, list[object]]]:
        classes = dict.fromkeys(type(o) for o in objs)
        return [(_meta_in(self._metas, cls), [o for o in objs if type(o) is cls]) for cls in classes]

    def _write(self, sql_for: Callable[[EntityMeta], str], objs: Sequence[object]) -> None:
        groups = self._grouped(objs)
        _check_idle(self._reading, [m.cls for m, _ in groups])
        with _transaction(self._conn):
            for m, group in groups:
                self._conn.executemany(sql_for(m), [_row(m, o) for o in group])


def _meta_in(metas: Mapping[type, EntityMeta], cls: type) -> EntityMeta:
    try:
        return metas[cls]
    except KeyError:
        raise KeyError(f"{cls.__name__} was not passed to inlay.open") from None


def _check_idle(reading: Counter[type], classes: Iterable[type]) -> None:
    """Raise if an iteration that is still open reads the table of one of `classes`."""
    for cls in classes:
        if reading[cls] > 0:
            raise RuntimeError(f"{cls.__name__} changed during iteration")


# ROLLBACK TO leaves the savepoint open, so it is released as well.
_TOP_LEVEL = (BEGIN, COMMIT, (ROLLBACK,))
_NESTED = (SAVEPOINT, RELEASE, (ROLLBACK_TO, RELEASE))


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Runs a new transaction, or a savepoint inside an open one, so a failed call leaves nothing behind.

    A new transaction takes the next transaction number, which the writes inside it use.
    """
    nested = conn.in_transaction
    begin, end, undo = _NESTED if nested else _TOP_LEVEL
    with _store_errors():
        conn.execute(begin)
        try:
            if not nested:
                _next_tx(conn)
            yield
            conn.execute(end)
        except BaseException:
            _execute_all(conn, undo)
            raise


def _next_tx(conn: sqlite3.Connection) -> None:
    (tx,) = conn.execute(NEXT_TX, (to_sql(datetime.now(UTC)),)).fetchone()
    if tx >= CURRENT:
        raise StoreError("the db has used every transaction number and cannot be written to")


@contextmanager
def _store_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.IntegrityError as e:
        match e.sqlite_errorname:
            case "SQLITE_CONSTRAINT_UNIQUE":
                raise AlreadyExists(str(e)) from e
            case "SQLITE_CONSTRAINT_FOREIGNKEY":
                raise BrokenRef("a Ref would point to a record that is not stored") from e
        raise


def _row(m: EntityMeta, obj: object) -> tuple[object, ...]:
    return tuple(to_sql(getattr(obj, f.name)) for f in m.fields)


def _from_row(m: EntityMeta, row: Sequence[object]) -> Any:
    return m.cls(**{f.name: from_sql(value, f.py_type) for f, value in zip(m.fields, row, strict=True)})


# Relative to the current directory, like any relative path.
DEFAULT_PATH: Final = ".inlay-history.db"


def open(entities: Iterable[type], *, path: str | os.PathLike[str] = DEFAULT_PATH) -> Db:
    metas = {m.cls: m for m in map(meta, entities)}
    _check_refs(metas)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _execute_all(conn, (*CONNECTION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        _check_tables(metas.values(), tables)
        create_tx = [CREATE_TX_TABLE] if TX_TABLE not in tables else []
        create_tables = [create_table_sql(m) for m in metas.values() if m.table not in tables]
        create_indexes = [create_index_sql(m, i) for m in metas.values() for i in m.indexes]
        version_indexes = [sql for m in metas.values() for sql in version_index_sqls(m)]
        _execute_all(conn, (*create_tx, *create_tables, *create_indexes, *version_indexes, COMMIT))
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

