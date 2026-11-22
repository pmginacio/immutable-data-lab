import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Self, overload

from inlay.conditions import own_value
from inlay.db import AlreadyExists, BrokenRef, Missing, SchemaMismatch
from inlay.functions import Aggregate, aggregate_of
from inlay.history.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    CREATE_TX_TABLE,
    LATEST_TX,
    LIST_INDEXES,
    LIST_TABLES,
    RECORD_TX,
    REFERRERS,
    RELEASE,
    ROLLBACK,
    ROLLBACK_TO,
    SAVEPOINT,
    TX_AT,
    TX_TABLE,
    aggregate_sql,
    clear_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    drop_index_sql,
    drop_table_sql,
    indexes_sql,
    insert_sql,
    select_sql,
    tx_time,
)
from inlay.meta import EntityMeta, meta, reachable
from inlay.meta import metas as entity_metas
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
    record_meta,
    result_type,
)
from inlay.types import from_sql, to_sql


@dataclass(frozen=True, eq=False)
class RecordView[T]:
    """A lazy, read-only collection of records. Every read runs its query again.

    It reads the records as they were at transaction `_tx`, or the current ones if it is None.
    """

    _conn: sqlite3.Connection
    _open_iterations: Counter[type]
    _query: Query
    _tx: int | None = None

    @property
    def _meta(self) -> EntityMeta:
        return record_meta(self._query)

    def __iter__(self) -> Iterator[T]:
        sql, params = select_sql(self._query, self._tx)
        classes = classes_read(self._query)
        # SQLite does not say what an open SELECT returns once its tables change, so writes to them are refused.
        self._open_iterations.update(classes)
        try:
            for row in self._conn.execute(sql, params):
                yield _from_row(self._meta, row)
        finally:
            self._open_iterations.subtract(classes)

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
        return self._aggregate(aggregate, self._aggregated(aggregate, value))

    @overload
    def min[V](self, value: V) -> V | None: ...
    @overload
    def min[V, D](self, value: V, default: D) -> V | D: ...
    def min(self, value: Any, default: Any = None) -> Any:
        return self._extreme(min, value, default)

    @overload
    def max[V](self, value: V) -> V | None: ...
    @overload
    def max[V, D](self, value: V, default: D) -> V | D: ...
    def max(self, value: Any, default: Any = None) -> Any:
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
        return default if result is None else from_sql(result, result_type(own))

    def _view(self, query: Query) -> "RecordView[T]":
        return RecordView(self._conn, self._open_iterations, query, self._tx)

    def _aggregate(self, aggregate: Aggregate[Any], value: Value | None = None) -> Any:
        sql, params = aggregate_sql(self._query, self._tx, aggregate, value)
        return self._conn.execute(sql, params).fetchone()[0]


@dataclass(frozen=True, eq=False)
class RecordSet[T](RecordView[T]):
    """All the records of one class. Unlike a view, it can also delete."""

    _known: set[type] = field(kw_only=True)  # the db's, which a rollback empties

    def delete(self, *keys: str) -> None:
        _raise_if_iterating(self._open_iterations, [self._meta.cls])
        with _transaction(self._conn, self._known):
            deleted = self._conn.executemany(delete_by_key_sql(self._meta), ((to_sql(k),) for k in keys)).rowcount
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self, *conds: Condition | bool) -> None:
        cond = all_of(*conds)
        check_condition(self._meta.cls, cond)
        sql, params = clear_sql(self._meta, cond)
        _raise_if_iterating(self._open_iterations, [self._meta.cls])
        with _transaction(self._conn, self._known):
            self._conn.execute(sql, params)


@dataclass(frozen=True, eq=False)
class Snapshot:
    """The db as it was at transaction `tx`: the same reads as the db, and no writes."""

    _conn: sqlite3.Connection
    _open_iterations: Counter[type]  # shared with the db, which writes on the same connection
    _checked: Callable[[type], EntityMeta]  # the db's check that a class has a matching table
    tx: int

    def __getitem__[T](self, cls: type[T]) -> RecordView[T]:
        return RecordView(self._conn, self._open_iterations, Table(self._checked(cls).cls), self.tx)


class Db:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._open_iterations: Counter[type] = Counter()
        # The classes whose tables this db has found in the file, matching them. Any rollback empties it,
        # since it may have undone a `register` or a `del`, and the next use of each class looks again.
        self._known: set[type] = set()

    def __getitem__[T](self, cls: type[T]) -> RecordSet[T]:
        """The records of `cls`. Raises `KeyError` if it has no table, and `SchemaMismatch` if its table differs."""
        return RecordSet(self._conn, self._open_iterations, Table(self._checked(cls).cls), _known=self._known)

    def __contains__(self, cls: type) -> bool:
        """Whether `cls` has a table, even one that differs from it."""
        return meta(cls).table in self._tables()

    def __delitem__(self, cls: type) -> None:
        """Drops the table of `cls`, with its whole history. Raises `BrokenRef` while another table points to it."""
        m = meta(cls)
        _raise_if_iterating(self._open_iterations, [cls])
        with _transaction(self._conn, self._known):
            if m.table not in self._tables():
                raise KeyError(f"no table in this db for {cls.__name__}")
            referrers = [t for (t,) in self._conn.execute(REFERRERS, (m.table,)) if t != m.table]
            if referrers:
                raise BrokenRef(f"{cls.__name__} cannot be deleted while tables {referrers} point to it")
            self._conn.execute(drop_table_sql(m.table))
            self._known.discard(cls)

    def register(self, *classes: type) -> None:
        """Creates the tables and indexes of `classes`. Raises `SchemaMismatch` if one of them has a table.

        Each class its `Ref`s point to must have a table already, or be one of `classes`.
        """
        new = entity_metas(classes)  # raises if two of them use the same table
        _refuse_optional(new.values())
        targets = {f.target for m in new.values() for f in m.fields if f.target is not None} - new.keys()
        with _transaction(self._conn, self._known):
            tables = self._tables()
            if stored := [m.cls.__name__ for m in new.values() if m.table in tables]:
                raise SchemaMismatch(f"a table is already in this db for {', '.join(stored)}")
            for target in targets:
                self._checked(target)  # raises if it has no table, or one that differs from it
            _execute(self._conn, (create_table_sql(m) for m in new.values()))
            _execute(self._conn, (sql for m in new.values() for sql in indexes_sql(m).values()))
            self._known.update(new)

    @property
    def tx(self) -> int:
        """The number of the latest committed transaction, or 0 before the first one.

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
        return Snapshot(self._conn, self._open_iterations, self._checked, tx)

    def transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn, self._known)

    def add(self, *objs: object) -> None:
        self._apply_by_entity(objs, _insert)

    def update(self, *objs: object) -> None:
        """Adds each record, or replaces the one with the same key, which stays in the history.

        The same key twice in one call raises `AlreadyExists`, while inlay's `update` keeps the last record.
        """
        self._apply_by_entity(objs, _delete_by_key, _insert)

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

    def _checked(self, cls: type) -> EntityMeta:
        """The meta of `cls`, once its table and those of the classes its `Ref`s reach are found to match them.

        The first time, it also creates the indexes that are missing and drops the ones no longer declared.
        """
        if cls not in self._known:
            metas = reachable(cls)
            tables = self._tables()
            if missing := [m.cls.__name__ for m in metas if m.table not in tables]:
                raise KeyError(f"no table in this db for {', '.join(missing)}; register it first")
            _check_tables(metas, tables)
            for m in metas:
                _sync_indexes(self._conn, m)
            self._known.update(m.cls for m in metas)
        return meta(cls)

    def _tables(self) -> dict[str, str]:
        return dict(self._conn.execute(LIST_TABLES))

    def _apply_by_entity(self, objs: Sequence[object], *writes: _Write) -> None:
        """Runs `writes`, in order, on the records of each class, all in one transaction."""
        groups = _grouped(objs)
        _raise_if_iterating(self._open_iterations, groups)
        with _transaction(self._conn, self._known):
            for cls, group in groups.items():
                m = self._checked(cls)
                for write in writes:
                    write(self._conn, m, group)


# Writes the records of one class. It runs inside the transaction, so raising undoes the whole call.
type _Write = Callable[[sqlite3.Connection, EntityMeta, list[object]], None]


def _insert(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    conn.executemany(insert_sql(m), _rows(m, group))


def _delete_by_key(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    """Ends the current versions with the keys of `group`, if there are any."""
    conn.executemany(delete_by_key_sql(m), ((to_sql(getattr(o, m.key.name)),) for o in group))


def _remove(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    deleted = conn.executemany(delete_equal_sql(m), _rows(m, group)).rowcount
    if deleted != len(group):
        raise Missing(f"{len(group) - deleted} of the {m.cls.__name__} records are not stored")


def _discard(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    conn.executemany(delete_equal_sql(m), _rows(m, group))


def _grouped(objs: Iterable[object]) -> dict[type, list[object]]:
    """The records of each class, so each class is written with one statement, in the order classes appear."""
    groups: defaultdict[type, list[object]] = defaultdict(list)
    for o in objs:
        groups[type(o)].append(o)
    return groups


def _raise_if_iterating(open_iterations: Counter[type], classes: Iterable[type]) -> None:
    """Raise if an iteration that is still open reads the table of one of `classes`."""
    for cls in classes:
        if open_iterations[cls] > 0:
            raise RuntimeError(f"{cls.__name__} changed during iteration")


# A new transaction records itself, with its number and time, just before it commits; a savepoint is part of it.
# ROLLBACK TO leaves the savepoint open, so it is released as well.
_TOP_LEVEL = (BEGIN, (RECORD_TX, COMMIT), ROLLBACK)
_NESTED = (SAVEPOINT, RELEASE, (ROLLBACK_TO, RELEASE))


@contextmanager
def _transaction(conn: sqlite3.Connection, known: set[type]) -> Iterator[None]:
    """Runs a new transaction, or a savepoint inside an open one, so a failed call leaves nothing behind.

    A rollback empties `known`, the classes the db found in the file, since it may have undone a change to the tables.
    """
    begin, end, undo = _NESTED if conn.in_transaction else _TOP_LEVEL
    with _store_errors():
        _execute(conn, begin)
        try:
            yield
            _execute(conn, end)
        except BaseException:
            _execute(conn, undo)
            known.clear()
            raise


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


def _rows(m: EntityMeta, objs: Iterable[object]) -> Iterator[tuple[object, ...]]:
    return (_row(m, o) for o in objs)


def _from_row(m: EntityMeta, row: Sequence[object]) -> Any:
    return m.cls(**{f.name: from_sql(value, f.py_type) for f, value in zip(m.fields, row, strict=True)})


DEFAULT_PATH = ".inlay-history.db"


def open(path: str | os.PathLike[str] = DEFAULT_PATH) -> Db:
    """The db in the file at `path`, created if there is none. `Db.register` creates the tables of classes."""
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _execute(conn, (*CONNECTION_SETUP, BEGIN))
        tables = dict(conn.execute(LIST_TABLES))
        if tables.get(TX_TABLE, CREATE_TX_TABLE) != CREATE_TX_TABLE:
            raise SchemaMismatch(f"table {TX_TABLE!r} is not the history db's table of transactions")
        create_tx = [CREATE_TX_TABLE] if TX_TABLE not in tables else []
        _execute(conn, (*create_tx, COMMIT))
    except BaseException:
        conn.close()  # also rolls back the schema changes
        raise
    return Db(conn)


def _execute(conn: sqlite3.Connection, statements: str | Iterable[str]) -> None:
    """Runs one statement, or several in order."""
    # A string is also an iterable of strings, one for each character, so it is checked first.
    for statement in (statements,) if isinstance(statements, str) else statements:
        conn.execute(statement)


def _refuse_optional(metas: Iterable[EntityMeta]) -> None:
    """The history db is paused, so its SQL has not learned NULLs."""
    if optional := [f"{m.cls.__name__}.{f.name}" for m in metas for f in m.fields if f.optional]:
        raise TypeError(f"the history db does not support optional fields yet: {', '.join(optional)}")


def _sync_indexes(conn: sqlite3.Connection, m: EntityMeta) -> None:
    """Creates the indexes of `m` that its table lacks, and drops the ones the history db made that `m` no longer has.

    Only indexes named as the history db names them, after the table, are dropped, so ones made by hand stay.
    """
    wanted = indexes_sql(m)
    stored = [name for name, _ in conn.execute(LIST_INDEXES, (m.table,)) if name.startswith(f"{m.table}__")]
    drops = [drop_index_sql(name) for name in stored if name not in wanted]
    creates = [sql for name, sql in wanted.items() if name not in stored]
    _execute(conn, (*drops, *creates))


def _check_tables(metas: Collection[EntityMeta], tables: Mapping[str, str]) -> None:
    """Raise if a table already in the db was created differently from what its entity needs now."""
    changed = [m for m in metas if m.table in tables and tables[m.table] != create_table_sql(m)]
    if changed:
        raise SchemaMismatch("\n".join(_mismatch(m, tables[m.table]) for m in changed))


def _mismatch(m: EntityMeta, stored_sql: str) -> str:
    return f"table {m.table!r} does not match its entity:\n  in db:  {stored_sql}\n  entity: {create_table_sql(m)}"

