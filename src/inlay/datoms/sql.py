from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from inlay.functions import Aggregate
from inlay.meta import EntityMeta, FieldMeta, meta
from inlay.nodes import (
    And,
    BinOp,
    Call,
    Compare,
    CompareOp,
    Condition,
    Filter,
    GroupValue,
    In,
    Match,
    Not,
    Or,
    Project,
    Node,
    RefValue,
    Slice,
    Sort,
    Table,
    Value,
    describe,
)
from inlay.types import to_sql

CONNECTION_SETUP = ("PRAGMA journal_mode = WAL",)
BEGIN = "BEGIN IMMEDIATE"
COMMIT = "COMMIT"
ROLLBACK = "ROLLBACK"
SAVEPOINT = "SAVEPOINT inlay"
RELEASE = "RELEASE inlay"
ROLLBACK_TO = "ROLLBACK TO inlay"
LIST_TABLES = "SELECT name, sql FROM sqlite_master WHERE type = 'table'"


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _text(value: str) -> str:
    """`value` as an SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


# The table of transactions, with the number and the time of each, in whole milliseconds since 1970 in UTC.
TX_TABLE = "_inlay_tx"
_TXS, _TX, _AT = quote(TX_TABLE), quote("tx"), quote("at")
CREATE_TX_TABLE = f"CREATE TABLE {_TXS} ({_TX} INTEGER PRIMARY KEY, {_AT} INTEGER NOT NULL) STRICT"
# The number the open transaction will get: it holds the write lock, so nobody else can take it first.
_THIS_TX = f"(SELECT coalesce(max({_TX}), 0) + 1 FROM {_TXS})"
# Records a transaction just before it commits: its number, which its writes used, at a time never earlier than the
# one before, even when the clock goes back. The latest time is the newest transaction's, found by number.
# SQLite's clock has milliseconds, and round undoes the float error of turning its seconds into milliseconds.
RECORD_TX = (
    f"INSERT INTO {_TXS} ({_TX}, {_AT}) SELECT {_THIS_TX}, "
    f"max(CAST(round(unixepoch('now', 'subsec') * 1000) AS INTEGER), "
    f"coalesce((SELECT {_AT} FROM {_TXS} ORDER BY {_TX} DESC LIMIT 1), 0))"
)
LATEST_TX = f"SELECT coalesce(max({_TX}), 0) FROM {_TXS}"
TX_AT = f"{LATEST_TX} WHERE {_AT} <= ?"

# The table of datoms, for every class: field `a` of record `e` of class `class` is `v` from transaction `tx` on,
# or no longer has a value if `added` is false. `class` is the table name of the class, and `e` is the record's key.
# The value of a field at a transaction is its newest datom at or before it. Datoms are only ever inserted,
# except that a second write to a field in the same transaction replaces the first.
# `v` takes any type, and keeps the one `to_sql` gave it.
DATOMS_TABLE = "_inlay_datoms"
_DATOMS = quote(DATOMS_TABLE)
_DATOM_KEY = "class, e, a, tx"
CREATE_DATOMS = f"""CREATE TABLE {_DATOMS} (
    class TEXT NOT NULL,
    e TEXT NOT NULL,
    a TEXT NOT NULL,
    v ANY NOT NULL,
    tx INTEGER NOT NULL,
    added INTEGER NOT NULL,
    PRIMARY KEY ({_DATOM_KEY})
) STRICT, WITHOUT ROWID"""
# Finds the datoms of the open transaction, which the checks of references read.
CREATE_TX_INDEX = f"CREATE INDEX IF NOT EXISTS {quote(DATOMS_TABLE + '__tx')} ON {_DATOMS} (tx)"

# The latest datom of each field of each record, without its transaction, so the present is read without the history.
# Every write puts the same rows in both tables. A removed record keeps its rows, with `added` false.
CURRENT_TABLE = "_inlay_current"
_CURRENT = quote(CURRENT_TABLE)
_CURRENT_KEY = "class, e, a"
CREATE_CURRENT = f"""CREATE TABLE {_CURRENT} (
    class TEXT NOT NULL,
    e TEXT NOT NULL,
    a TEXT NOT NULL,
    v ANY NOT NULL,
    added INTEGER NOT NULL,
    PRIMARY KEY ({_CURRENT_KEY})
) STRICT, WITHOUT ROWID"""

# The rows of the current table as they were at each transaction `at_tx` that a snapshot read on this connection.
# It is temporary: the past never changes, so each connection can keep its own copy, and SQLite drops it on close.
_SNAPSHOTS = "temp." + quote("_inlay_at")
CREATE_SNAPSHOTS = f"""CREATE TABLE IF NOT EXISTS {_SNAPSHOTS} (
    at_tx INTEGER NOT NULL,
    class TEXT NOT NULL,
    e TEXT NOT NULL,
    a TEXT NOT NULL,
    v ANY NOT NULL,
    added INTEGER NOT NULL,
    PRIMARY KEY (at_tx, {_CURRENT_KEY})
) STRICT, WITHOUT ROWID"""
# A row if the snapshot table has rows at transaction ?1. Only a transaction with no datoms up to it has none,
# and filling it again reads no datoms, through the `tx` index.
SNAPSHOT_FILLED = f"SELECT 1 FROM {_SNAPSHOTS} WHERE at_tx = ?1 LIMIT 1"
# Fills the snapshot table at transaction ?1 with the newest datom of each field at or before it.
FILL_SNAPSHOT = f"""
    INSERT INTO {_SNAPSHOTS} (at_tx, class, e, a, v, added)
    SELECT ?1, d.class, d.e, d.a, d.v, d.added FROM {_DATOMS} AS d
    WHERE d.tx <= ?1
        AND NOT EXISTS (
            SELECT 1 FROM {_DATOMS} AS n
            WHERE n.class = d.class
                AND n.e = d.e
                AND n.a = d.a
                AND n.tx > d.tx
                AND n.tx <= ?1
        )"""


def tx_time(at: datetime) -> int:
    """`at`, which has a timezone, in the unit of the stored transaction times, cut to the millisecond as SQLite's are."""
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def ref_index_sql(m: EntityMeta, f: FieldMeta) -> str:
    """An index of the current values of `Ref` field `f`, which finds the records that point to a record."""
    return f"""
        CREATE INDEX IF NOT EXISTS {_ref_index(m, f)} ON {_CURRENT} (v)
        WHERE class = {_text(m.table)}
            AND a = {_text(f.name)}"""


def _ref_index(m: EntityMeta, f: FieldMeta) -> str:
    return quote("__".join((CURRENT_TABLE, m.table, f.name)))


def _stored(target: EntityMeta, e: str) -> str:
    """Whether the record of `target` with key `e`, which is SQL, is stored."""
    return f"""EXISTS (
        SELECT 1 FROM {_CURRENT}
        WHERE class = {_text(target.table)}
            AND e = {e}
            AND a = {_text(target.key.name)}
            AND added
    )"""


# Each write is a pair of statements, run in turn for each record: its datoms, then the same rows in the current table.
type Writes = tuple[str, str]


def add_sql(m: EntityMeta) -> Writes:
    """Adds the datoms of a record from the values of its fields, unless a record with its key is stored."""
    return _write_sql(m, f"NOT {_stored(m, _param(m, m.key))}")


def update_sql(m: EntityMeta) -> Writes:
    """Adds the datoms of a record from the values of its fields, except those that are its fields' values already."""
    current = f"""(
        SELECT v FROM {_CURRENT}
        WHERE class = {_text(m.table)}
            AND e = {_param(m, m.key)}
            AND a = new.a
            AND added
    )"""
    return _write_sql(m, f"new.v IS NOT {current}")


def _write_sql(m: EntityMeta, where: str) -> Writes:
    """Adds a datom for each field `new.a` of a record, with value `new.v`, that meets `where`.

    It takes the values of the record's fields in order.
    """
    values = ", ".join(f"({_text(f.name)}, {_param(m, f)})" for f in m.fields)
    return _into_both(f"""
        WITH new(a, v) AS (VALUES {values})
        SELECT {_text(m.table)}, {_param(m, m.key)}, new.a, new.v, 1 FROM new
        WHERE {where}""")


def _into_both(rows: str) -> Writes:
    """Writes the rows of `rows`, a SELECT of (class, e, a, v, added), as datoms of this transaction and as current.

    `rows` reads the current table only, which the first statement does not change, so both pick the same rows.
    """
    # The WITH of `rows` stays in a subquery, because sqlite3 reports no row count for a statement that starts
    # with WITH. A SELECT before ON CONFLICT needs a WHERE, or SQLite reads ON as part of a join.
    return (
        f"""
        INSERT INTO {_DATOMS} (class, e, a, v, added, tx)
        SELECT *, {_THIS_TX} FROM ({rows})
        WHERE true
        ON CONFLICT ({_DATOM_KEY}) DO UPDATE SET v = excluded.v, added = excluded.added""",
        f"""
        INSERT INTO {_CURRENT} (class, e, a, v, added)
        SELECT * FROM ({rows})
        WHERE true
        ON CONFLICT ({_CURRENT_KEY}) DO UPDATE SET v = excluded.v, added = excluded.added""",
    )


def _param(m: EntityMeta, f: FieldMeta) -> str:
    """The parameter of field `f` among the values of the fields of `m`."""
    return f"?{m.fields.index(f) + 1}"


def delete_by_key_sql(m: EntityMeta) -> Writes:
    """Retracts the fields of the stored record with a key at this transaction."""
    return _retract_sql(m, f"{_column(m.key)} = ?")


def delete_equal_sql(m: EntityMeta) -> Writes:
    """Retracts the fields of the stored record equal to a record at this transaction."""
    return _retract_sql(m, " AND ".join(f"{_column(f)} = ?" for f in m.fields))


def clear_sql(m: EntityMeta, cond: Condition) -> tuple[Writes, list[object]]:
    """Retracts the fields of every stored record that matches `cond` at this transaction, and its parameters."""
    where, params = condition_sql(cond, None)
    return _retract_sql(m, where), params


def _retract_sql(m: EntityMeta, where: str) -> Writes:
    """Retracts every field of the stored records that meet `where`, a condition on their fields.

    Each retraction keeps the value it retracts, as in Datomic.
    """
    records, _ = _rows_sql(m, None)  # the current records take no parameters
    return _into_both(f"""
        SELECT c.class, c.e, c.a, c.v, 0 FROM {_CURRENT} AS c
        WHERE c.class = {_text(m.table)}
            AND c.added
            AND c.e IN (SELECT {_column(m.key)} FROM ({records}) WHERE {where})""")


def dangling_ref_sql(m: EntityMeta, f: FieldMeta, target: EntityMeta) -> str:
    """A row if a value of `Ref` field `f`, added in this transaction, points to no stored record of `target`."""
    return f"""
        SELECT 1 FROM {_DATOMS} AS r
        WHERE r.tx = {_THIS_TX}
            AND r.class = {_text(m.table)}
            AND r.a = {_text(f.name)}
            AND r.added
            AND NOT {_stored(target, 'r.v')}
        LIMIT 1"""


def referenced_removal_sql(m: EntityMeta, f: FieldMeta, target: EntityMeta) -> str:
    """A row if a record of `target`, removed in this transaction, is still the value of a stored `Ref` field `f`."""
    # Without statistics SQLite walks every current row of the class by its primary key instead.
    referrers = f"""
        SELECT 1 FROM {_CURRENT} AS r INDEXED BY {_ref_index(m, f)}
        WHERE r.class = {_text(m.table)}
            AND r.a = {_text(f.name)}
            AND r.v = k.e
            AND r.added"""
    return f"""
        SELECT 1 FROM {_DATOMS} AS k
        WHERE k.tx = {_THIS_TX}
            AND k.class = {_text(target.table)}
            AND k.a = {_text(target.key.name)}
            AND NOT k.added
            AND EXISTS ({referrers})
        LIMIT 1"""


def _rows_sql(m: EntityMeta, tx: int | None) -> tuple[str, list[object]]:
    """A SELECT of the records of `m` at transaction `tx`, from the snapshot table, which must be filled at `tx`,
    or of the current ones, from the current table, and its parameters.

    It groups only once, by `e`, so SQLite can push a condition on the key down to the rows of one record;
    through a second grouping, a `get` would rebuild the whole class.
    """
    source, at = (_CURRENT, "") if tx is None else (_SNAPSHOTS, "AND c.at_tx = ?")
    fields = ", ".join(
        f"c.e AS {_column(f)}" if f.is_key else f"max(CASE c.a WHEN {_text(f.name)} THEN c.v END) AS {_column(f)}"
        for f in m.fields
    )
    sql = f"""
        SELECT
            {fields}
        FROM {source} AS c
        WHERE c.class = {_text(m.table)}
            {at}
            AND c.added
        GROUP BY c.e"""
    return sql, [] if tx is None else [tx]


def _sql_op(op: CompareOp) -> str:
    return "=" if op == "==" else op


def condition_sql(cond: Condition, tx: int | None) -> tuple[str, list[object]]:
    """The SQL for `cond`, and its parameters in order. `cond` was checked by `check_condition`.

    The records it reaches through relations are read at transaction `tx`, or the current ones if it is None.
    """
    match cond:
        case Compare(left, op, right):
            left_sql, left_params = value_sql(left)
            right_sql, right_params = value_sql(right)
            return f"{left_sql} {_sql_op(op)} {right_sql}", left_params + right_params
        case Match(field, "startswith", text):
            return f"substr({_column(field)}, 1, ?) = ?", [len(text), text]
        case Match(field, "endswith", text):
            column = _column(field)
            return f"substr({column}, length({column}) - ? + 1) = ?", [len(text), text]
        case Match(field, "contains", text):
            return f"instr({_column(field)}, ?) > 0", [text]
        case In(field, Filter() as records):
            keys, params = _keys_sql(records, tx)
            return f"{_column(field)} IN ({keys})", params
        case In(field, tuple() as values):
            marks = ", ".join("?" * len(values))
            return f"{_column(field)} IN ({marks})", [to_sql(v) for v in values]
        case And(items):
            return _join(items, " AND ", "1", tx)
        case Or(items):
            return _join(items, " OR ", "0", tx)
        case Not(item):
            sql, params = condition_sql(item, tx)
            return f"NOT ({sql})", params
        case _:
            raise TypeError(f"expected a condition, got {cond!r}")


def _join(items: Sequence[Condition], operator: str, empty: str, tx: int | None) -> tuple[str, list[object]]:
    parts = [condition_sql(item, tx) for item in items]
    if not parts:
        return empty, []
    return f"({operator.join(sql for sql, _ in parts)})", [p for _, params in parts for p in params]


def _keys_sql(records: Filter, tx: int | None) -> tuple[str, list[object]]:
    """A SELECT of the keys of `records` at transaction `tx`, and its parameters."""
    select = _select(records, tx)
    return _render(select, _column(select.m.key))


def value_sql(value: object) -> tuple[str, list[object]]:
    """The SQL for a `Value`, or a parameter for a plain value, and its parameters in order."""
    match value:
        case FieldMeta():
            return _column(value), []
        case Call(fn, arg):
            sql, params = value_sql(arg)
            return fn.sql.format(sql), params
        case BinOp(op, left, right):
            left_sql, left_params = value_sql(left)
            right_sql, right_params = value_sql(right)
            if op == "/":
                # SQLite divides two integers without a remainder; Python's `/` does not.
                left_sql = f"CAST({left_sql} AS REAL)"
            return f"({left_sql} {op} {right_sql})", left_params + right_params
        case RefValue():
            raise TypeError(f"the datoms version does not support values read through a Ref yet: {describe(value)}")
        case GroupValue():
            raise TypeError("the datoms version does not support values of groups yet")
        case _:
            return "?", [to_sql(value)]


def _column(field: FieldMeta) -> str:
    return quote(field.name)


def select_sql(query: Node, tx: int | None) -> tuple[str, list[object]]:
    """A SELECT of every column of the records `query` picks at transaction `tx`, and its parameters in order.

    With no `tx`, it picks from the current records.
    """
    select = _select(query, tx)
    return _render(select, _columns(select.m))


def aggregate_sql(
    query: Node,
    tx: int | None,
    aggregate: Aggregate[Any],
    value: Value | None = None,
    keys: tuple[Value, ...] = (),
) -> tuple[str, list[object]]:
    """`aggregate` over the records `query` picks at transaction `tx`, of `value` if it takes one.

    With `keys`, one row for each distinct key, sorted by key: the values of `keys`, then the aggregate.
    """
    inner, inner_params = select_sql(query, tx)
    parts = [value_sql(k) for k in keys]
    value_part, value_params = ("", []) if value is None else value_sql(value)
    columns = [sql for sql, _ in parts] + [aggregate.sql.format(value_part)]
    sql = f"SELECT {', '.join(columns)} FROM ({inner})"
    if keys:
        # Numbers stand for the columns of the result, so each key and its parameters appear only once.
        positions = ", ".join(str(i) for i in range(1, len(keys) + 1))
        sql += f" GROUP BY {positions} ORDER BY {positions}"
    return sql, [p for _, params in parts for p in params] + value_params + inner_params


@dataclass(frozen=True)
class _Select:
    """The parts of one SELECT, which takes its records from `source`: a table, or a query in parentheses.

    `tx` is the transaction its records are read at, also through relations, or None for the current ones.
    """

    m: EntityMeta
    tx: int | None
    source: str
    source_params: tuple[object, ...] = ()
    where: tuple[Condition, ...] = ()
    order: tuple[tuple[Value, bool], ...] = ()  # each key with whether it sorts descending
    slice: tuple[int, int | None] | None = None


def _select(query: Node, tx: int | None) -> _Select:
    match query:
        case Table(cls):
            m = meta(cls)
            sql, params = _rows_sql(m, tx)
            return _Select(m, tx, f"({sql})", tuple(params))
        case Filter(source, cond):
            select = _open(_select(source, tx))
            return replace(select, where=(*select.where, cond))
        case Sort(source, keys, reverse):
            select = _open(_select(source, tx))
            return replace(select, order=tuple((k, reverse) for k in keys) + select.order)
        case Slice(source, start, stop):
            return replace(_open(_select(source, tx)), slice=(start, stop))
        case Project():
            raise TypeError("the paused history experiments do not read projections")


def _open(select: _Select) -> _Select:
    """`select` if it can still take conditions, an order or a slice, or else a new SELECT on top of it.

    A slice comes last in a SELECT, so whatever comes after a slice works on a SELECT of the slice.
    """
    if select.slice is None:
        return select
    sql, params = _render(select, _columns(select.m))
    # SQL keeps no order from a subquery, so the outer SELECT sorts again.
    return _Select(select.m, select.tx, f"({sql})", tuple(params), order=select.order)


def _render(select: _Select, columns: str) -> tuple[str, list[object]]:
    sql = f"SELECT {columns} FROM {select.source}"
    params = list(select.source_params)
    if select.where:
        cond = select.where[0] if len(select.where) == 1 else And(select.where)
        where, where_params = condition_sql(cond, select.tx)
        sql += f" WHERE {where}"
        params += where_params
    if select.order:
        order = []
        for key, descending in select.order:
            key_sql, key_params = value_sql(key)
            order.append(key_sql + (" DESC" if descending else ""))
            params += key_params
        if not any(key == select.m.key for key, _ in select.order):
            # The key breaks ties, so slices of the same order never repeat or skip a record.
            order.append(_column(select.m.key))
        sql += f" ORDER BY {', '.join(order)}"
    if select.slice is not None:
        start, stop = select.slice
        sql += " LIMIT ? OFFSET ?"
        params += [-1 if stop is None else max(stop - start, 0), start]
    return sql, params


def _columns(m: EntityMeta) -> str:
    return ", ".join(map(_column, m.fields))
