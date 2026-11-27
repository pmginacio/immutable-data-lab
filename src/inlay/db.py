import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, ItemsView, Iterable, Iterator, Mapping, Sequence, ValuesView
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Self, cast, overload

from inlay.conditions import Expr, group_column, group_condition, own_value, record_values
from inlay.entity import projection_of, restored
from inlay.functions import Aggregate, aggregate_of
from inlay.meta import EntityMeta, FieldMeta, meta, reachable, table_name
from inlay.meta import metas as entity_metas
from inlay.nodes import (
    as_condition,
    Compare,
    Condition,
    Filter,
    GroupValue,
    Project,
    Node,
    Slice,
    Sort,
    Table,
    Value,
    all_of,
    check_condition,
    classes_read,
    describe,
    group,
    group_value,
    record_meta,
    result_type,
    root_group,
)
from inlay.sql import (
    BEGIN,
    COMMIT,
    CONNECTION_SETUP,
    LIST_INDEXES,
    LIST_TABLES,
    REFERRERS,
    RELEASE,
    ROLLBACK,
    ROLLBACK_TO,
    SAVEPOINT,
    aggregate_sql,
    clear_sql,
    copy_rows_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    drop_index_sql,
    drop_table_sql,
    index_prefix,
    indexes_sql,
    insert_sql,
    rename_table_sql,
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


@dataclass(frozen=True, eq=False)
class Query[T]:
    """Records of one class to read, described by `filter`, `sorted`, slicing and `project`, which give a new query.

    A query bound to a db runs when it is read: iterated, counted, or asked whether it holds a record.
    `db[Order]` is bound, and so is every query built from it; `db[q]` binds any query, such as one from `query(Order)`.
    Every read runs it again.
    """

    _node: Node
    _db: Db | None = None

    @property
    def _meta(self) -> EntityMeta:
        return record_meta(self._node)

    def __iter__(self) -> Iterator[T]:
        db = _bound(self._db)
        sql, params = select_sql(self._node)
        classes = classes_read(self._node)
        # SQLite does not say what an open SELECT returns once its tables change, so writes to them are refused.
        db._open_iterations.update(classes)
        try:
            for row in db._conn.execute(sql, params):
                yield _from_row(self._meta, row)
        finally:
            db._open_iterations.subtract(classes)

    def __len__(self) -> int:
        return self._aggregate(aggregate_of(len))

    def __contains__(self, record: object) -> bool:
        if type(record) is not self._meta.cls:
            return False
        equal = (Compare(f, "==", getattr(record, f.name)) for f in self._meta.fields)
        return len(self.filter(all_of(*equal))) > 0

    def __getitem__(self, s: slice) -> "Query[T]":
        if not isinstance(s, slice):
            raise TypeError("a query can only be sliced, as in q[:10]")
        return Query(Slice(self._node, *_slice_bounds(s)), self._db)

    def filter(self, cond: Condition | bool) -> "Query[T]":
        cond = as_condition(cond)
        check_condition(self._meta.cls, cond)
        return Query(Filter(self._node, cond), self._db)

    def sorted(self, *, key: Any, reverse: bool = False) -> "Query[T]":
        """`key` is a value such as `Order.total`, `Order.total * 2` or `Order.customer.name`, or a tuple of values.

        A value read through an empty `Ref` is None, which sorts first.
        """
        keys = key if isinstance(key, tuple) else (key,)
        return Query(Sort(self._node, tuple(own_value(self._meta.cls, k) for k in keys), reverse), self._db)

    def get(self, key: str) -> T | None:
        return next(iter(self.filter(Compare(self._meta.key, "==", key))), None)

    def project[P](self, cls: type[P]) -> "Query[P]":
        """These records in the shape of the projection `cls`, whose fields filters and sorting use after it."""
        projection = projection_of(cls)
        if projection.root is not self._meta.cls:
            root, here = projection.root.__name__, self._meta.cls.__name__
            raise TypeError(f"{cls.__name__} is a projection of {root}, not of {here}")
        source = self._node
        while isinstance(source, Filter):
            source = source.source
        if not isinstance(source, Table):
            # A subquery keeps no order in SQL, so an order or a slice of the root would be lost.
            raise ValueError("project comes after filters only; sort and slice after it, by its fields")
        return Query(Project(self._node, projection), self._db)

    @overload
    def sum(self, value: Expr[int]) -> int: ...
    @overload
    def sum(self, value: Expr[float]) -> float: ...
    def sum(self, value: Any) -> Any:
        aggregate = aggregate_of(sum)
        return self._aggregate(aggregate, self._aggregated(aggregate, value))

    @overload
    def min[V](self, value: Expr[V]) -> V | None: ...
    @overload
    def min[V, D](self, value: Expr[V], default: D) -> V | D: ...
    def min(self, value: Any, default: Any = None) -> Any:
        return self._extreme(min, value, default)

    @overload
    def max[V](self, value: Expr[V]) -> V | None: ...
    @overload
    def max[V, D](self, value: Expr[V], default: D) -> V | D: ...
    def max(self, value: Any, default: Any = None) -> Any:
        return self._extreme(max, value, default)

    # A `Ref` field is typed as its class on the class, so it can lead to that class's fields; its groups hold keys.
    @overload
    def aggregate_by[R](self, key: type, func: Callable[..., R], value: Any = None) -> "Grouped[str, R]": ...
    @overload
    def aggregate_by[K, R](self, key: Expr[K], func: Callable[..., R], value: Any = None) -> "Grouped[K, R]": ...
    # A tuple of values gives tuples of their types; typing cannot say that for any length, so two and three are spelled out.
    @overload
    def aggregate_by[K1, K2, R](
        self,
        key: tuple[Expr[K1], Expr[K2]],
        func: Callable[..., R],
        value: Any = None,
    ) -> "Grouped[tuple[K1, K2], R]": ...
    @overload
    def aggregate_by[K1, K2, K3, R](
        self,
        key: tuple[Expr[K1], Expr[K2], Expr[K3]],
        func: Callable[..., R],
        value: Any = None,
    ) -> "Grouped[tuple[K1, K2, K3], R]": ...
    @overload
    def aggregate_by[R](self, key: tuple[Any, ...], func: Callable[..., R], value: Any = None) -> "Grouped[tuple[Any, ...], R]": ...
    def aggregate_by(self, key: Any, func: Callable[..., Any], value: Any = None) -> "Grouped[Any, Any]":
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
        return Grouped(group(self._node, aggregate, own, keys), many, self._db)

    def _aggregated(self, aggregate: Aggregate[Any], value: object) -> Value:
        """`value` as a value of the records of this query that `aggregate` can take."""
        own = own_value(self._meta.cls, value)
        if aggregate.arg is None:
            raise TypeError(f"{aggregate.name} takes no value")
        if not aggregate.takes(t := result_type(own)):
            raise TypeError(f"{aggregate.name} cannot take {describe(own)}, a {t.__name__}")
        return own

    def _extreme(self, func: Callable[..., Any], value: Any, default: object) -> Any:
        aggregate = aggregate_of(func)
        own = self._aggregated(aggregate, value)
        result = self._aggregate(aggregate, own)
        return default if result is None else from_sql(result, result_type(own))

    def _aggregate(self, aggregate: Aggregate[Any], value: Value | None = None) -> Any:
        sql, params = aggregate_sql(self._node, aggregate, value)
        return _bound(self._db)._conn.execute(sql, params).fetchone()[0]


def query[T](cls: type[T]) -> Query[T]:
    """All the records of the entity `cls`, as a query not bound to a db: `db[q]` runs it."""
    return Query(Table(meta(cls).cls))


def _slice_bounds(s: slice) -> tuple[int, int | None]:
    start, stop = s.start or 0, s.stop
    if s.step is not None or start < 0 or (stop is not None and stop < 0):
        raise ValueError("a slice takes a start and a stop, and no negative numbers")
    return start, stop


def _bound(db: Db | None) -> Db:
    if db is None:
        raise TypeError("this query has no db; run it as db[q]")
    return db


@dataclass(frozen=True, eq=False, repr=False)
class Grouped[K, V](Mapping[K, V]):
    """What `aggregate_by` gives: a value for each key, in key order, as a query of groups.

    `filter`, `sorted` and slicing give new groups, as they give new queries; their conditions and sort keys use
    `value`, and the keys as they were grouped by, as in `totals.filter(totals.value > 100, Order.customer != "c1")`.
    Bound to a db, it is also a read-only mapping from each key to its value, and every read runs its query again.

    Given a field instead of a key, as in `totals[Order.customer]`, it gives the value of each record's own key
    for use in queries, as in `db[Order].filter(Order.total == biggest[Order.customer])`;
    the value is None for a key with no group.
    """

    _node: Node  # a `Group`, or a query that filters, sorts or slices one
    _many: bool  # keys are tuples, as `aggregate_by` was given a tuple of values
    _db: Db | None = None

    @property
    def value(self) -> Expr[V]:
        """The value of each group, for conditions and sort keys."""
        return Expr((), group_value(self._node))

    @overload
    def __getitem__(self, key: slice) -> "Grouped[K, V]": ...
    @overload
    def __getitem__(self, key: K) -> V: ...
    # The value of each record's own group, None for a record with no group, as in `totals[Customer.id]`;
    # a `Ref` field, as in `totals[Order.customer]`, is typed as its class on the class.
    @overload
    def __getitem__(self, key: type | Expr[Any] | tuple[Expr[Any], ...]) -> Expr[V | None]: ...
    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return replace(self, _node=Slice(self._node, *_slice_bounds(key)))
        if (found := record_values(key if self._many and isinstance(key, tuple) else (key,))) is not None:
            return self._looked_up(*found)
        keys = key if self._many else (key,)
        columns = record_meta(self._node).fields[:-1]
        if not isinstance(keys, tuple) or len(keys) != len(columns):
            raise KeyError(key)
        row = next(self._rows(Filter(self._node, all_of(*map(Compare, columns, ("==",) * len(keys), keys)))), None)
        if row is None:
            raise KeyError(key)
        return row[1]

    def __iter__(self) -> Iterator[K]:
        return iter(self._read())

    def __len__(self) -> int:
        sql, params = aggregate_sql(self._node, aggregate_of(len))
        return _bound(self._db)._conn.execute(sql, params).fetchone()[0]

    def items(self) -> ItemsView[K, V]:
        return self._read().items()

    def values(self) -> ValuesView[V]:
        return self._read().values()

    def filter(self, cond: Condition | bool) -> "Grouped[K, V]":
        cond = group_condition(root_group(self._node), as_condition(cond))
        return replace(self, _node=Filter(self._node, cond))

    def sorted(self, *, key: Any, reverse: bool = False) -> "Grouped[K, V]":
        """`key` is `value`, a key as it was grouped by, or a tuple of them; ties keep key order."""
        keys = key if isinstance(key, tuple) else (key,)
        groups = root_group(self._node)
        return replace(self, _node=Sort(self._node, tuple(group_column(groups, k) for k in keys), reverse))

    def _looked_up(self, relations: tuple[FieldMeta, ...], at: tuple[Value, ...]) -> Any:
        columns = record_meta(self._node).fields[:-1]
        if len(at) != len(columns):
            raise TypeError(f"the keys are {len(columns)} values, got {len(at)}")
        for value, c in zip(at, columns):
            if (t := result_type(value)) is not c.py_type:
                raise TypeError(f"{describe(value)} is a {t.__name__}, but the keys are a {c.py_type.__name__}")
        return Expr(relations, GroupValue(self._node, at))

    def _rows(self, node: Node) -> Iterator[tuple[Any, Any]]:
        """The key and value of each group of `node`, in its order; a key is a tuple when there are several."""
        sql, params = select_sql(node)
        columns = record_meta(node).fields
        for row in _bound(self._db)._conn.execute(sql, params):
            *keys, value = map(from_sql, row, (c.py_type for c in columns))
            yield (tuple(keys) if self._many else keys[0]), value

    def _read(self) -> dict[Any, Any]:
        return dict(self._rows(self._node))


@dataclass(frozen=True, eq=False)
class RecordSet[T](Query[T]):
    """All the records of one class, in a db. Unlike other queries, it can also delete.

    What `filter`, `sorted` and the others give is a plain `Query`, so it cannot delete.
    """

    def delete(self, *keys: str) -> None:
        db = _bound(self._db)
        _raise_if_iterating(db._open_iterations, [self._meta.cls])
        with db.transaction():
            deleted = db._conn.executemany(delete_by_key_sql(self._meta), ((k,) for k in keys)).rowcount
            if deleted != len(keys):
                raise Missing(f"{len(keys) - deleted} of the {self._meta.cls.__name__} keys are not stored")

    def clear(self, cond: Condition | bool = all_of()) -> None:
        cond = as_condition(cond)
        check_condition(self._meta.cls, cond)
        sql, params = clear_sql(self._meta, cond)
        db = _bound(self._db)
        _raise_if_iterating(db._open_iterations, [self._meta.cls])
        with db.transaction():
            db._conn.execute(sql, params)


class Db:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._open_iterations: Counter[type] = Counter()
        # The classes whose tables this db has found in the file, matching them. Any rollback empties it,
        # since it may have undone a `register` or a `del`, and the next use of each class looks again.
        self._known: set[type] = set()

    @overload
    def __getitem__[T](self, x: type[T]) -> RecordSet[T]: ...
    @overload
    def __getitem__[T](self, x: Query[T]) -> Query[T]: ...
    @overload
    def __getitem__[K, V](self, x: Grouped[K, V]) -> Grouped[K, V]: ...
    def __getitem__(self, x: Any) -> Any:
        """The records of the class `x`, or the query or group `x` bound to this db.

        Raises `KeyError` if a class read has no table, and `SchemaMismatch` if its table differs.
        """
        match x:
            case type():
                self._require_tables(x)
                return RecordSet(Table(x), self)
            case Query():
                self._require_tables(*classes_read(x._node))
                return Query(x._node, self)
            case Grouped():
                self._require_tables(*classes_read(x._node))
                return replace(x, _db=self)
        raise TypeError(f"db[...] takes a class, a query or a group, not {x!r}")

    def __contains__(self, cls: type) -> bool:
        """Whether `cls` has a table, even one that differs from it."""
        return meta(cls).table in self._tables()

    def __delitem__(self, cls: type) -> None:
        """Drops the table of `cls` and its records. Raises `BrokenRef` while another table points to it."""
        m = meta(cls)
        _raise_if_iterating(self._open_iterations, [cls])
        with self.transaction():
            if m.table not in self._tables():
                raise KeyError(f"no table in this db for {cls.__name__}")
            if referrers := self._referrers(m.table):
                raise BrokenRef(f"{cls.__name__} cannot be deleted while tables {referrers} point to it")
            self._conn.execute(drop_table_sql(m.table))
            self._known.discard(cls)

    def register(self, *classes: type) -> None:
        """Creates the tables and indexes of `classes`. Raises `SchemaMismatch` if one of them has a table.

        Each class its `Ref`s point to must have a table already, or be one of `classes`.
        """
        new = entity_metas(classes)  # raises if two of them use the same table
        targets = {f.target for m in new.values() for f in m.fields if f.target is not None} - new.keys()
        with self.transaction():
            tables = self._tables()
            if stored := [m.cls.__name__ for m in new.values() if m.table in tables]:
                raise SchemaMismatch(f"a table is already in this db for {', '.join(stored)}")
            self._require_tables(*targets)
            _execute(self._conn, (create_table_sql(m) for m in new.values()))
            _execute(self._conn, (sql for m in new.values() for sql in indexes_sql(m).values()))
            self._known.update(new)

    def migrate(self, fn: Callable[[Db], None], old: type, new: type) -> None:
        """Change the shape of the table of `new` and convert its records with `fn`, all or nothing.

        `old` describes the table as it is stored, under another name, so `fn` can read its records.
        Nothing happens if the table already matches `new`. `rename` changes the name of a table.
        """
        old_meta, new_meta = meta(old), meta(new)
        if old_meta.table == new_meta.table:
            raise ValueError(f"{old.__name__} and {new.__name__} use the same table; give the old class another name")
        with self.transaction():
            stored = self._tables().get(new_meta.table)
            if stored == create_table_sql(new_meta):
                return
            if stored != create_table_sql(old_meta, new_meta.table):
                raise SchemaMismatch(
                    f"cannot migrate {old.__name__} to {new.__name__}: the db needs a table {new_meta.table!r} "
                    f"that matches {old.__name__}; a migration keeps the name of the table, and rename changes it"
                )
            key_changes = (old_meta.key.name, old_meta.key.py_type) != (new_meta.key.name, new_meta.key.py_type)
            if key_changes and (referrers := self._referrers(new_meta.table)):
                raise SchemaMismatch(
                    f"cannot migrate {old.__name__} to {new.__name__}: "
                    f"the key changes, and tables {referrers} point to it"
                )
            # The references of other tables name the table, so they lead to the new records once the old ones move.
            self.register(old)
            self._conn.execute(copy_rows_sql(new_meta.table, old_meta))
            self._conn.execute(drop_table_sql(new_meta.table))
            # Classes found matching the stored table, or reaching it through a `Ref`, no longer do.
            self._known.clear()
            self.register(new)
            fn(self)
            del self[old]

    def rename(self, old: str, new: type) -> None:
        """Renames the table of the class that was called `old` to the table of `new`, keeping its records.

        The references to it in other tables and the names of its indexes follow, and then it must match `new`.
        Nothing happens if the old table is gone and the table of `new` matches it.
        """
        old_table, m = table_name(old), meta(new)
        with self.transaction():
            tables = self._tables()
            if old_table not in tables and tables.get(m.table) == create_table_sql(m):
                return
            if old_table not in tables:
                raise SchemaMismatch(f"there is no table {old_table!r} to rename")
            if m.table in tables:
                raise SchemaMismatch(f"table {m.table!r} already exists")
            # With foreign keys on, as on every connection of the db, SQLite also renames the references to the table.
            self._conn.execute(rename_table_sql(old_table, m.table))
            for name, sql in self._conn.execute(LIST_INDEXES, (m.table,)).fetchall():
                if name.startswith(index_prefix(old_table)):
                    _execute(self._conn, reprefix_index_sql(name, sql, old_table, m.table))
            # Classes found matching the old table, or reaching it through a `Ref`, no longer do.
            self._known.clear()
            self._require_tables(new)

    def transaction(self) -> AbstractContextManager[None]:
        return _transaction(self._conn, self._known)

    def add(self, *objs: object) -> None:
        self._apply_by_entity(objs, _insert)

    def update(self, *objs: object) -> None:
        self._apply_by_entity(objs, _upsert)

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

    def _require_tables(self, *classes: type) -> None:
        """Raises unless `classes`, and the classes their `Ref`s reach, have tables that match them.

        Each class is looked up once. Its first use also creates the declared indexes that are missing,
        and drops the ones inlay made that are no longer declared.
        """
        metas = {m.cls: m for c in classes if c not in self._known for m in reachable(c)}
        if not metas:
            return
        tables = self._tables()
        if missing := [m.cls.__name__ for m in metas.values() if m.table not in tables]:
            raise KeyError(f"no table in this db for {', '.join(missing)}; register it first")
        _check_tables(metas.values(), tables)
        for m in metas.values():
            _sync_indexes(self._conn, m)
        self._known.update(metas)

    def _tables(self) -> dict[str, str]:
        return dict(self._conn.execute(LIST_TABLES))

    def _referrers(self, table: str) -> list[str]:
        """The other tables with a foreign key to `table`."""
        return [t for (t,) in self._conn.execute(REFERRERS, (table,)) if t != table]

    def _apply_by_entity(self, objs: Sequence[object], write: _Write) -> None:
        """Runs `write` on the records of each class, all in one transaction."""
        groups = _grouped(objs)
        _raise_if_iterating(self._open_iterations, groups)
        # Before the transaction, so a failed write does not undo the indexes a first use creates.
        self._require_tables(*groups)
        with self.transaction():
            for cls, group in groups.items():
                write(self._conn, meta(cls), group)


# Writes the records of one class. It runs inside the transaction, so raising undoes the whole call.
type _Write = Callable[[sqlite3.Connection, EntityMeta, list[object]], None]


def _insert(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    conn.executemany(insert_sql(m), _rows(m, group))


def _upsert(conn: sqlite3.Connection, m: EntityMeta, group: list[object]) -> None:
    conn.executemany(upsert_sql(m), _rows(m, group))


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


# ROLLBACK TO leaves the savepoint open, so it is released as well.
_TOP_LEVEL = (BEGIN, COMMIT, ROLLBACK)
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
            case "SQLITE_CONSTRAINT_PRIMARYKEY":
                raise AlreadyExists(str(e)) from e
            case "SQLITE_CONSTRAINT_FOREIGNKEY":
                raise BrokenRef("a Ref would point to a record that is not stored") from e
        raise


def _row(m: EntityMeta, obj: object) -> tuple[object, ...]:
    return tuple(to_sql(getattr(obj, f.name)) for f in m.fields)


def _rows(m: EntityMeta, objs: Iterable[object]) -> Iterator[tuple[object, ...]]:
    return (_row(m, o) for o in objs)


def _from_row(m: EntityMeta, row: Sequence[object]) -> Any:
    return restored(m.cls, {f.name: from_sql(value, f.py_type) for f, value in zip(m.fields, row, strict=True)})


# Relative to the current directory, like any relative path.
DEFAULT_PATH = ".inlay.db"


def open(path: str | os.PathLike[str] = DEFAULT_PATH) -> Db:
    """The db in the file at `path`, created if there is none. `Db.register` creates the tables of classes."""
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _execute(conn, CONNECTION_SETUP)
    except BaseException:
        conn.close()
        raise
    return Db(conn)


def _execute(conn: sqlite3.Connection, statements: str | Iterable[str]) -> None:
    """Runs one statement, or several in order."""
    # A string is also an iterable of strings, one for each character, so it is checked first.
    for statement in (statements,) if isinstance(statements, str) else statements:
        conn.execute(statement)


def _sync_indexes(conn: sqlite3.Connection, m: EntityMeta) -> None:
    """Creates the indexes `m` declares that its table lacks, and drops the ones inlay made that it no longer declares.

    Only indexes named as inlay names them, after the table, are dropped, so ones made by hand stay.
    """
    declared = indexes_sql(m)
    stored = [name for name, _ in conn.execute(LIST_INDEXES, (m.table,)) if name.startswith(index_prefix(m.table))]
    drops = [drop_index_sql(name) for name in stored if name not in declared]
    creates = [sql for name, sql in declared.items() if name not in stored]
    _execute(conn, (*drops, *creates))


def _check_tables(metas: Collection[EntityMeta], tables: Mapping[str, str]) -> None:
    """Raise if a table already in the db was created differently from what its entity needs now."""
    changed = [m for m in metas if m.table in tables and tables[m.table] != create_table_sql(m)]
    if changed:
        raise SchemaMismatch("\n".join(_mismatch(m, tables[m.table]) for m in changed))


def _mismatch(m: EntityMeta, stored_sql: str) -> str:
    return f"table {m.table!r} does not match its entity:\n  in db:  {stored_sql}\n  entity: {create_table_sql(m)}"

