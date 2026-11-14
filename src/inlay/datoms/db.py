import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Self, overload

from inlay.conditions import own_value
from inlay.db import AlreadyExists, BrokenRef, Missing, SchemaMismatch
from inlay.functions import Aggregate, aggregate_of
from inlay.datoms.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    CREATE_DATOMS,
    CREATE_TX_INDEX,
    CREATE_TX_TABLE,
    DATOMS_TABLE,
    LATEST_TX,
    LIST_TABLES,
    RECORD_TX,
    RELEASE,
    ROLLBACK,
    ROLLBACK_TO,
    SAVEPOINT,
    TX_AT,
    TX_TABLE,
    add_sql,
    aggregate_sql,
    clear_sql,
    dangling_ref_sql,
    delete_by_key_sql,
    delete_equal_sql,
    ref_index_sql,
    referenced_removal_sql,
    select_sql,
    tx_time,
    update_sql,
)
from inlay.meta import EntityMeta, meta, metas as _metas
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

_MISSING = object()


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
        sql, params = aggregate_sql(self._query, self._tx, aggregate, own, keys)
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
        sql, params = aggregate_sql(self._query, self._tx, aggregate, value)
        return self._conn.execute(sql, params).fetchone()[0]


@dataclass(frozen=True, eq=False)
class RecordSet[T](RecordView[T]):
    """All the records of one class. Unlike a view, it can also delete."""

    _ref_checks: tuple[str, ...] = field(kw_only=True)

    def delete(self, *keys: str) -> None:
        _check_idle(self._reading, [self._meta.cls])
        with _transaction(self._conn, self._ref_checks):
            cursor = self._conn.executemany(delete_by_key_sql(self._meta), ((to_sql(k),) for k in keys))
            deleted = _records(self._meta, cursor)
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self, *conds: Condition | bool) -> None:
        cond = all_of(*conds)
        check_condition(self._meta.cls, cond)
        sql, params = clear_sql(self._meta, cond)
        _check_idle(self._reading, [self._meta.cls])
        with _transaction(self._conn, self._ref_checks):
            self._conn.execute(sql, params)


@dataclass(frozen=True, eq=False)
class Snapshot:
    """The db as it was at transaction `tx`: the same reads as the db, and no writes."""

    _conn: sqlite3.Connection
    _reading: Counter[type]  # shared with the db, which writes on the same connection
    _metas: frozendict[type, EntityMeta]
    tx: int

    def __getitem__[T](self, cls: type[T]) -> RecordView[T]:
        return RecordView(self._conn, self._reading, Table(_meta_in(self._metas, cls).cls), self.tx)


class Db:
    def __init__(
        self, conn: sqlite3.Connection, metas: frozendict[type, EntityMeta], ref_checks: tuple[str, ...]
    ) -> None:
        self._conn = conn
        self._metas = metas
        self._ref_checks = ref_checks  # SELECTs that return a row if a reference is broken, run before commit
        self._reading: Counter[type] = Counter()

    def __getitem__[T](self, cls: type[T]) -> RecordSet[T]:
        table = Table(_meta_in(self._metas, cls).cls)
        return RecordSet(self._conn, self._reading, table, _ref_checks=self._ref_checks)

    @property
    def tx(self) -> int:
        """The number of the latest committed transaction, or 0 before the first write.

        A transaction is only recorded as it commits, so one that is still open is never counted.
        """
        return self._conn.execute(LATEST_TX).fetchone()[0]

    def as_of(self, at: int | datetime) -> Snapshot:
        """The db as it was at transaction `at`, or at the last transaction at or before the time `at`.

        Transaction times come from SQLite's clock, to the millisecond,
        so a transaction in the same millisecond as the time `at` counts as at or before it.
        """
        if isinstance(at, datetime):
            if at.tzinfo is None:
                raise ValueError("as_of needs a datetime with a timezone, because transaction times are in UTC")
            tx = self._conn.execute(TX_AT, (tx_time(at),)).fetchone()[0]
        else:
            latest = self.tx
            if not 0 <= at <= latest:
                raise ValueError(f"there is no committed transaction {at}; the latest is {latest}")
            tx = at
        return Snapshot(self._conn, self._reading, self._metas, tx)

    def transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn, self._ref_checks)

    def add(self, *objs: object) -> None:
        self._apply_by_entity(objs, _add)

    def update(self, *objs: object) -> None:
        """Adds each record, or replaces the one with the same key, which stays in the history.

        Only the fields that change get a datom, so an update to an equal record adds none,
        though it still takes a transaction number.
        """
        self._apply_by_entity(objs, _update)

    def remove(self, *objs: object) -> None:
        self._apply_by_entity(objs, _remove)

    def discard(self, *objs: object) -> None:
        self._apply_by_entity(objs, _discard)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _apply_by_entity(self, objs: Sequence[object], write: _Write) -> None:
        """Runs `write` on the records of each class, all in one transaction."""
        groups = _grouped(objs)
        _check_idle(self._reading, groups)
        with _transaction(self._conn, self._ref_checks):
            for cls, group in groups.items():
                write(self._conn, _meta_in(self._metas, cls), group)


# Writes the records of one class. It runs inside the transaction, so raising undoes the whole call.
type _Write = Callable[[sqlite3.Connection, EntityMeta, list[object]], None]


def _add(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    added = _records(m, conn.executemany(add_sql(m), _rows(m, group)))
    if added != len(group):
        raise AlreadyExists(f"{len(group) - added} of the {m.cls.__name__} keys are stored already")


def _update(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    conn.executemany(update_sql(m), _rows(m, group))


def _remove(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    deleted = _records(m, conn.executemany(delete_equal_sql(m), _rows(m, group)))
    if deleted != len(group):
        raise Missing(f"{len(group) - deleted} of the {m.cls.__name__} records are not stored")


def _discard(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    conn.executemany(delete_equal_sql(m), _rows(m, group))


def _records(m: EntityMeta, cursor: sqlite3.Cursor) -> int:
    """How many records of `m` the writes of `cursor` added or retracted.

    Each one writes a datom for every field, which holds while stored records have the fields of the class;
    `open` does not check that yet, so after a class changes the count is wrong.
    """
    return cursor.rowcount // len(m.fields)


def _grouped(objs: Iterable[object]) -> dict[type, list[object]]:
    """The records of each class, so each class is written with one statement, in the order classes appear."""
    groups: defaultdict[type, list[object]] = defaultdict(list)
    for o in objs:
        groups[type(o)].append(o)
    return groups


def _meta_in(metas: Mapping[type, EntityMeta], cls: type) -> EntityMeta:
    try:
        return metas[cls]
    except KeyError:
        raise KeyError(f"{cls.__name__} was not passed to inlay.datoms.open") from None


def _check_idle(reading: Counter[type], classes: Iterable[type]) -> None:
    """Raise if an iteration that is still open reads the table of one of `classes`."""
    for cls in classes:
        if reading[cls] > 0:
            raise RuntimeError(f"{cls.__name__} changed during iteration")


# A new transaction records itself, with its number and time, just before it commits; a savepoint is part of it.
# ROLLBACK TO leaves the savepoint open, so it is released as well.
_TOP_LEVEL = (BEGIN, (RECORD_TX, COMMIT), ROLLBACK)
_NESTED = (SAVEPOINT, RELEASE, (ROLLBACK_TO, RELEASE))


@contextmanager
def _transaction(conn: sqlite3.Connection, ref_checks: Iterable[str]) -> Iterator[None]:
    """Runs a new transaction, or a savepoint inside an open one, so a failed call leaves nothing behind.

    A new transaction runs `ref_checks` before it commits, so the order of writes inside it does not matter.
    """
    top_level = not conn.in_transaction
    begin, end, undo = _TOP_LEVEL if top_level else _NESTED
    _execute(conn, begin)
    try:
        yield
        if top_level and any(conn.execute(sql).fetchone() for sql in ref_checks):
            raise BrokenRef("a Ref would point to a record that is not stored")
        _execute(conn, end)
    except BaseException:
        _execute(conn, undo)
        raise


def _row(m: EntityMeta, obj: object) -> tuple[object, ...]:
    return tuple(to_sql(getattr(obj, f.name)) for f in m.fields)


def _rows(m: EntityMeta, objs: Iterable[object]) -> Iterator[tuple[object, ...]]:
    return (_row(m, o) for o in objs)


def _from_row(m: EntityMeta, row: Sequence[object]) -> Any:
    return m.cls(**{f.name: from_sql(value, f.py_type) for f, value in zip(m.fields, row, strict=True)})


DEFAULT_PATH = ".inlay-datoms.db"
_OWN_TABLES = ((TX_TABLE, CREATE_TX_TABLE), (DATOMS_TABLE, CREATE_DATOMS))


def open(entities: Iterable[type], *, path: str | os.PathLike[str] = DEFAULT_PATH) -> Db:
    metas = _metas(entities)
    _check_refs(metas)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _execute(conn, (*CONNECTION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        for name, create in _OWN_TABLES:
            if tables.get(name, create) != create:
                raise SchemaMismatch(f"table {name!r} was not made by inlay.datoms")
        create_tables = [create for name, create in _OWN_TABLES if name not in tables]
        refs = [(m, f, metas[f.target]) for m in metas.values() for f in m.fields if f.target is not None]
        ref_indexes = [ref_index_sql(m, f) for m, f, _ in refs]
        _execute(conn, (*create_tables, CREATE_TX_INDEX, *ref_indexes, COMMIT))
    except BaseException:
        conn.close()  # also rolls back the schema changes
        raise
    ref_checks = [check(m, f, target) for m, f, target in refs for check in (dangling_ref_sql, referenced_removal_sql)]
    return Db(conn, metas, tuple(ref_checks))


def _execute(conn: sqlite3.Connection, statements: str | Iterable[str]) -> None:
    """Runs one statement, or several in order."""
    # A string is also an iterable of strings, one for each character, so it is checked first.
    for statement in (statements,) if isinstance(statements, str) else statements:
        conn.execute(statement)


def _check_refs(metas: Mapping[type, EntityMeta]) -> None:
    dangling = [
        f"{m.cls.__name__}.{f.name} -> {f.target.__name__}"
        for m in metas.values()
        for f in m.fields
        if f.target is not None and f.target not in metas
    ]
    if dangling:
        raise TypeError(f"Ref fields point to entities not passed to inlay.datoms.open: {', '.join(dangling)}")

