from collections.abc import Sequence
from dataclasses import dataclass, replace
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
    In,
    Match,
    Not,
    Or,
    Query,
    Slice,
    Sort,
    Table,
    Value,
)
from inlay.types import SQL_TYPES, to_sql

CONNECTION_SETUP = ("PRAGMA foreign_keys = ON", "PRAGMA journal_mode = WAL")
BEGIN = "BEGIN IMMEDIATE"
COMMIT = "COMMIT"
ROLLBACK = "ROLLBACK"
SAVEPOINT = "SAVEPOINT inlay"
RELEASE = "RELEASE inlay"
ROLLBACK_TO = "ROLLBACK TO inlay"
LIST_TABLES = "SELECT name, sql FROM sqlite_master WHERE type = 'table'"


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# The columns that every table of versions has: the transaction where a version starts, and where it ends.
_TX_FROM = "tx_from"
_TX_TO = "tx_to"
# The `tx_to` of a version that is still current. A transaction never gets this number.
CURRENT = 2**63 - 1
_IS_CURRENT = f"{quote(_TX_TO)} = {CURRENT}"

# The table of transactions, with the number and the UTC time of each.
# The check keeps a transaction from taking the number that marks a current version.
TX_TABLE = "inlay_tx"
CREATE_TX_TABLE = (
    f'CREATE TABLE {quote(TX_TABLE)} ("tx" INTEGER PRIMARY KEY CHECK ("tx" < {CURRENT}), "at" TEXT NOT NULL) STRICT'
)
# Starts a transaction: the next number, at a time never earlier than the one before, even when the clock goes back.
NEXT_TX = (
    f"INSERT INTO {quote(TX_TABLE)} (\"at\") "
    f"SELECT max(strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), coalesce(max(\"at\"), '')) FROM {quote(TX_TABLE)}"
)
LATEST_TX = f'SELECT coalesce(max("tx"), 0) FROM {quote(TX_TABLE)}'
# julianday reads both SQLite's times and Python's, which differ in their digits of a second.
TX_AT = f'{LATEST_TX} WHERE julianday("at") <= julianday(?)'
# Every top-level transaction adds its row first and holds the write lock, so the largest number is its own.
_THIS_TX = f'(SELECT max("tx") FROM {quote(TX_TABLE)})'


def create_table_sql(m: EntityMeta) -> str:
    """The CREATE TABLE statement for `m`.

    SQLite keeps this text, and `open` compares it with what this function returns now,
    so any change to it, even in spacing, makes existing files fail the schema check.

    Each row is one version of a record, current from transaction `tx_from` until `tx_to`.
    The `live_` columns hold the key and the references of current versions only, so SQLite checks them as in inlay.
    """
    tx_columns = [f"{quote(name)} INTEGER NOT NULL" for name in (_TX_FROM, _TX_TO)]
    live = [_live_def(f) for f in m.fields if f.is_key or f.target is not None]
    columns = [*map(_column_def, m.fields), *tx_columns, *live]
    return f"CREATE TABLE {quote(m.table)} ({', '.join(columns)}) STRICT"


def create_index_sql(m: EntityMeta, index: tuple[FieldMeta, ...]) -> str:
    """A declared index, after `tx_to`, so that reading the current versions is an exact match on its first column."""
    name = "__".join((m.table, *(f.name for f in index)))
    columns = ", ".join(map(_column, index))
    return f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {quote(m.table)} ({quote(_TX_TO)}, {columns})"


def version_index_sqls(m: EntityMeta) -> list[str]:
    """The indexes every class needs: to read all of it, to read the versions of a record, and to check references.

    SQLite needs an index that starts with the column of a foreign key, or a removal scans the referring table.
    """
    table = quote(m.table)
    columns = [(_TX_TO, _TX_FROM), (m.key.name, _TX_FROM)]
    columns += [(_live(f),) for f in m.fields if f.target is not None]
    return [
        f"CREATE INDEX IF NOT EXISTS {quote('__'.join((m.table, *c)))} ON {table} ({', '.join(map(quote, c))})"
        for c in columns
    ]


def _column_def(f: FieldMeta) -> str:
    return f"{_column(f)} {SQL_TYPES[f.py_type]} NOT NULL"


def _live_def(f: FieldMeta) -> str:
    """The column that holds `f` on the current version and NULL on the others, which SQLite leaves unchecked."""
    sql = (
        f"{quote(_live(f))} {SQL_TYPES[f.py_type]} GENERATED ALWAYS AS "
        f"(CASE WHEN {_IS_CURRENT} THEN {_column(f)} END) VIRTUAL"
    )
    if f.target is None:  # the key
        return sql + " UNIQUE"
    target = meta(f.target)
    # Checked at commit, so the order of writes inside a transaction does not matter.
    return sql + f" REFERENCES {quote(target.table)} ({quote(_live(target.key))}) DEFERRABLE INITIALLY DEFERRED"


def _live(f: FieldMeta) -> str:
    return f"live_{f.name}"


def insert_sql(m: EntityMeta) -> str:
    """Adds a version that is current from this transaction on."""
    marks = ", ".join("?" * len(m.fields))
    tx_columns = f"{quote(_TX_FROM)}, {quote(_TX_TO)}"
    return f"INSERT INTO {quote(m.table)} ({_columns(m)}, {tx_columns}) VALUES ({marks}, {_THIS_TX}, {CURRENT})"


def delete_by_key_sql(m: EntityMeta) -> str:
    """Ends the current version with a key at this transaction. Nothing is deleted."""
    return _close_sql(m, f"{_column(m.key)} = ?")


def delete_equal_sql(m: EntityMeta) -> str:
    """Ends the current version equal to a record at this transaction. Nothing is deleted."""
    return _close_sql(m, " AND ".join(f"{_column(f)} = ?" for f in m.fields))


def clear_sql(m: EntityMeta, cond: Condition) -> tuple[str, list[object]]:
    """Ends every current version that matches `cond` at this transaction, and its parameters."""
    where, params = condition_sql(cond)
    return _close_sql(m, where), params


def _close_sql(m: EntityMeta, where: str) -> str:
    return f"UPDATE {quote(m.table)} SET {quote(_TX_TO)} = {_THIS_TX} WHERE {_IS_CURRENT} AND {where}"


def _sql_op(op: CompareOp) -> str:
    return "=" if op == "==" else op


def condition_sql(cond: Condition, tx: int | None = None) -> tuple[str, list[object]]:
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
        case _:
            return "?", [to_sql(value)]


def _column(field: FieldMeta) -> str:
    return quote(field.name)


def select_sql(query: Query, tx: int | None = None) -> tuple[str, list[object]]:
    """A SELECT of every column of the records `query` picks at transaction `tx`, and its parameters in order.

    With no `tx`, it picks from the current records.
    """
    select = _select(query, tx)
    return _render(select, _columns(select.m))


def aggregate_sql(
    query: Query,
    aggregate: Aggregate[Any],
    value: Value | None = None,
    keys: tuple[Value, ...] = (),
    tx: int | None = None,
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


def _select(query: Query, tx: int | None) -> _Select:
    match query:
        case Table(cls):
            m = meta(cls)
            table = f"SELECT {_columns(m)} FROM {quote(m.table)}"
            if tx is None:
                return _Select(m, tx, f"({table} WHERE {_IS_CURRENT})")
            at_tx = f"{quote(_TX_FROM)} <= ? AND ? < {quote(_TX_TO)}"
            return _Select(m, tx, f"({table} WHERE {at_tx})", (tx, tx))
        case Filter(source, cond):
            select = _open(_select(source, tx))
            return replace(select, where=(*select.where, cond))
        case Sort(source, keys, reverse):
            select = _open(_select(source, tx))
            return replace(select, order=tuple((k, reverse) for k in keys) + select.order)
        case Slice(source, start, stop):
            return replace(_open(_select(source, tx)), slice=(start, stop))


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
