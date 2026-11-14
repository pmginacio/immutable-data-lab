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
CREATE_DATOMS = f"""CREATE TABLE {_DATOMS} (
    class TEXT NOT NULL,
    e TEXT NOT NULL,
    a TEXT NOT NULL,
    v ANY NOT NULL,
    tx INTEGER NOT NULL,
    added INTEGER NOT NULL,
    PRIMARY KEY (class, e, a, tx)
) STRICT, WITHOUT ROWID"""
# Finds the datoms of the open transaction, which the checks of references read.
CREATE_TX_INDEX = f"CREATE INDEX IF NOT EXISTS {quote(DATOMS_TABLE + '__tx')} ON {_DATOMS} (tx)"
_DATOM_COLUMNS = "class, e, a, v, tx, added"
_UPSERT = "ON CONFLICT (class, e, a, tx) DO UPDATE SET v = excluded.v, added = excluded.added"


def tx_time(at: datetime) -> int:
    """`at`, which has a timezone, in the unit of the stored transaction times, cut to the millisecond as SQLite's are."""
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def ref_index_sql(m: EntityMeta, f: FieldMeta) -> str:
    """An index of the datoms of `Ref` field `f` by value, which finds the records that point to a record."""
    return f"""
        CREATE INDEX IF NOT EXISTS {_ref_index(m, f)} ON {_DATOMS} (class, a, v, e, tx)
        WHERE class = {_text(m.table)}
            AND a = {_text(f.name)}"""


def _ref_index(m: EntityMeta, f: FieldMeta) -> str:
    return quote("__".join((DATOMS_TABLE, m.table, f.name)))


def _newest(expr: str, cls: str, e: str, a: str) -> str:
    """`expr` over the newest datom of field `a` of record `e` of class `cls`, all SQL, or NULL if it has none."""
    return f"""(
        SELECT {expr} FROM {_DATOMS}
        WHERE class = {cls}
            AND e = {e}
            AND a = {a}
        ORDER BY tx DESC LIMIT 1
    )"""


def _is_live(target: EntityMeta, e: str) -> str:
    """Whether the record of `target` with key `e`, which is SQL, is stored: its key's newest datom is added."""
    return f"coalesce({_newest('added', _text(target.table), e, _text(target.key.name))}, 0)"


def add_sql(m: EntityMeta) -> str:
    """Adds the datoms of a record from the values of its fields, unless a record with its key is stored."""
    return _write_sql(m, f"NOT {_is_live(m, _param(m, m.key))}")


def update_sql(m: EntityMeta) -> str:
    """Adds the datoms of a record from the values of its fields, except those that are its fields' values already."""
    same = _newest("added AND v IS new.v", _text(m.table), _param(m, m.key), "new.a")
    return _write_sql(m, f"NOT coalesce({same}, 0)")


def _write_sql(m: EntityMeta, where: str) -> str:
    """Adds a datom for each field `new.a` of a record, with value `new.v`, that meets `where`.

    It takes the values of the record's fields in order.
    """
    values = ", ".join(f"({_text(f.name)}, {_param(m, f)})" for f in m.fields)
    # The WITH goes after INSERT, because sqlite3 reports no row count for a statement that starts with WITH.
    return f"""
        INSERT INTO {_DATOMS} ({_DATOM_COLUMNS})
        WITH new(a, v) AS (VALUES {values})
        SELECT {_text(m.table)}, {_param(m, m.key)}, new.a, new.v, {_THIS_TX}, 1 FROM new
        WHERE {where}
        {_UPSERT}"""


def _param(m: EntityMeta, f: FieldMeta) -> str:
    """The parameter of field `f` among the values of the fields of `m`."""
    return f"?{m.fields.index(f) + 1}"


def delete_by_key_sql(m: EntityMeta) -> str:
    """Retracts the fields of the stored record with a key at this transaction."""
    return _retract_sql(m, f"{_column(m.key)} = ?")


def delete_equal_sql(m: EntityMeta) -> str:
    """Retracts the fields of the stored record equal to a record at this transaction."""
    return _retract_sql(m, " AND ".join(f"{_column(f)} = ?" for f in m.fields))


def clear_sql(m: EntityMeta, cond: Condition) -> tuple[str, list[object]]:
    """Retracts the fields of every stored record that matches `cond` at this transaction, and its parameters."""
    where, params = condition_sql(cond, None)
    return _retract_sql(m, where), params


def _retract_sql(m: EntityMeta, where: str) -> str:
    """Retracts every field of the stored records that meet `where`, a condition on their fields.

    Each retraction keeps the value it retracts, as in Datomic, taken from the rebuilt records,
    one row for each record and field.
    """
    rows, _ = _rows_sql(m, None)  # the current records take no parameters
    names = ", ".join(f"({_text(f.name)})" for f in m.fields)
    value = " ".join(f"WHEN {_text(f.name)} THEN r.{_column(f)}" for f in m.fields)
    # `where` names fields without a table, and no field starts with an underscore, so `_name` cannot clash.
    return f"""
        INSERT INTO {_DATOMS} ({_DATOM_COLUMNS})
        WITH f(_name) AS (VALUES {names})
        SELECT {_text(m.table)}, r.{_column(m.key)}, f._name, CASE f._name {value} END, {_THIS_TX}, 0
        FROM ({rows}) AS r, f
        WHERE {where}
        {_UPSERT}"""


def dangling_ref_sql(m: EntityMeta, f: FieldMeta, target: EntityMeta) -> str:
    """A row if a value of `Ref` field `f`, added in this transaction, points to no stored record of `target`."""
    return f"""
        SELECT 1 FROM {_DATOMS} AS r
        WHERE r.tx = {_THIS_TX}
            AND r.class = {_text(m.table)}
            AND r.a = {_text(f.name)}
            AND r.added
            AND NOT {_is_live(target, 'r.v')}
        LIMIT 1"""


def referenced_removal_sql(m: EntityMeta, f: FieldMeta, target: EntityMeta) -> str:
    """A row if a record of `target`, removed in this transaction, is still the value of a stored `Ref` field `f`."""
    newest_tx = _newest("tx", "r.class", "r.e", "r.a")
    # Without statistics SQLite walks every datom of the class by its primary key instead.
    referrers = f"""
        SELECT 1 FROM {_DATOMS} AS r INDEXED BY {_ref_index(m, f)}
        WHERE r.class = {_text(m.table)}
            AND r.a = {_text(f.name)}
            AND r.v = k.e
            AND r.added
            AND r.tx = {newest_tx}"""
    return f"""
        SELECT 1 FROM {_DATOMS} AS k
        WHERE k.tx = {_THIS_TX}
            AND k.class = {_text(target.table)}
            AND k.a = {_text(target.key.name)}
            AND NOT k.added
            AND EXISTS ({referrers})
        LIMIT 1"""


def _rows_sql(m: EntityMeta, tx: int | None) -> tuple[str, list[object]]:
    """A SELECT of the records of `m` at transaction `tx`, or of the current ones, rebuilt from their newest datoms,
    and its parameters.

    It groups only once, so SQLite can push a condition on the key down to the datoms of one record;
    through a second grouping, a `get` would rebuild the whole class.
    """

    def at(alias: str) -> str:
        return "" if tx is None else f" AND {alias}.tx <= ?"

    newer = f"""
        SELECT 1 FROM {_DATOMS} AS n
        WHERE n.class = d.class
            AND n.e = d.e
            AND n.a = d.a
            AND n.tx > d.tx{at('n')}"""
    fields = ", ".join(
        f"d.e AS {_column(f)}" if f.is_key else f"max(CASE d.a WHEN {_text(f.name)} THEN d.v END) AS {_column(f)}"
        for f in m.fields
    )
    sql = f"""
        SELECT
            {fields}
        FROM {_DATOMS} AS d
        WHERE d.class = {_text(m.table)}{at('d')}
            AND NOT EXISTS ({newer})
        GROUP BY d.e
        HAVING max(CASE d.a WHEN {_text(m.key.name)} THEN d.added END)"""
    return sql, [] if tx is None else [tx, tx]


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
        case _:
            return "?", [to_sql(value)]


def _column(field: FieldMeta) -> str:
    return quote(field.name)


def select_sql(query: Query, tx: int | None) -> tuple[str, list[object]]:
    """A SELECT of every column of the records `query` picks at transaction `tx`, and its parameters in order.

    With no `tx`, it picks from the current records.
    """
    select = _select(query, tx)
    return _render(select, _columns(select.m))


def aggregate_sql(
    query: Query,
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


def _select(query: Query, tx: int | None) -> _Select:
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
