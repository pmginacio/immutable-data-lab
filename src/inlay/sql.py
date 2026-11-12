from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

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

type Aggregate = Literal["COUNT", "SUM", "MIN", "MAX"]


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def create_table_sql(m: EntityMeta) -> str:
    """Written exactly the way SQLite stores it, so it can be compared with an existing table."""
    return f"CREATE TABLE {quote(m.table)} ({', '.join(map(_column_def, m.fields))}) STRICT"


def create_index_sql(m: EntityMeta, index: tuple[FieldMeta, ...]) -> str:
    name = "__".join((m.table, *(f.name for f in index)))
    columns = ", ".join(quote(f.name) for f in index)
    return f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {quote(m.table)} ({columns})"


def _column_def(f: FieldMeta) -> str:
    sql = f"{quote(f.name)} {SQL_TYPES[f.py_type]} NOT NULL"
    if f.is_key:
        sql += " PRIMARY KEY"
    if f.target is not None:
        target = meta(f.target)
        # Checked at commit, so the order of writes inside a transaction does not matter.
        sql += f" REFERENCES {quote(target.table)} ({quote(target.key.name)}) DEFERRABLE INITIALLY DEFERRED"
    return sql


def insert_sql(m: EntityMeta) -> str:
    return f"INSERT INTO {quote(m.table)} ({_columns(m)}) VALUES ({', '.join('?' * len(m.fields))})"


def upsert_sql(m: EntityMeta) -> str:
    updates = ", ".join(f"{quote(f.name)} = excluded.{quote(f.name)}" for f in m.fields if not f.is_key)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return f"{insert_sql(m)} ON CONFLICT ({quote(m.key.name)}) {action}"


def delete_by_key_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)} WHERE {quote(m.key.name)} = ?"


def delete_equal_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)} WHERE {' AND '.join(f'{quote(f.name)} = ?' for f in m.fields)}"


def clear_sql(m: EntityMeta, cond: Condition) -> tuple[str, list[object]]:
    """A DELETE of every record that matches `cond`, and its parameters."""
    where, params = condition_sql(cond)
    return f"DELETE FROM {quote(m.table)} WHERE {where}", params


def _sql_op(op: CompareOp) -> str:
    return "=" if op == "==" else op


def condition_sql(cond: Condition) -> tuple[str, list[object]]:
    """The SQL for `cond`, and its parameters in order. `cond` was checked by `check_condition`."""
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
            keys, params = _keys_sql(records)
            return f"{_column(field)} IN ({keys})", params
        case In(field, tuple() as values):
            marks = ", ".join("?" * len(values))
            return f"{_column(field)} IN ({marks})", [to_sql(v) for v in values]
        case And(items):
            return _join(items, " AND ", "1")
        case Or(items):
            return _join(items, " OR ", "0")
        case Not(item):
            sql, params = condition_sql(item)
            return f"NOT ({sql})", params
        case _:
            raise TypeError(f"expected a condition, got {cond!r}")


def _join(items: Sequence[Condition], operator: str, empty: str) -> tuple[str, list[object]]:
    parts = [condition_sql(item) for item in items]
    if not parts:
        return empty, []
    return f"({operator.join(sql for sql, _ in parts)})", [p for _, params in parts for p in params]


def _keys_sql(records: Filter) -> tuple[str, list[object]]:
    """A SELECT of the keys of `records`, and its parameters."""
    select = _select(records)
    return _render(select, quote(select.m.key.name))


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


def select_sql(query: Query) -> tuple[str, list[object]]:
    """A SELECT of every column of the records `query` picks, and its parameters in order."""
    select = _select(query)
    return _render(select, _columns(select.m))


def aggregate_sql(query: Query, function: Aggregate, field: FieldMeta | None = None) -> tuple[str, list[object]]:
    """One value computed over the records `query` picks: all of them for COUNT, else `field`, a field of their own."""
    inner, params = select_sql(query)
    column = "*" if field is None else _column(field)
    return f"SELECT {function}({column}) FROM ({inner})", params


@dataclass(frozen=True, slots=True)
class _Select:
    """The parts of one SELECT, which takes its records from `source`: a table, or a query in parentheses."""

    m: EntityMeta
    source: str
    source_params: tuple[object, ...] = ()
    where: tuple[Condition, ...] = ()
    order: tuple[tuple[FieldMeta, bool], ...] = ()  # each field with whether it sorts descending
    slice: tuple[int, int | None] | None = None


def _select(query: Query) -> _Select:
    match query:
        case Table(cls):
            m = meta(cls)
            return _Select(m, quote(m.table))
        case Filter(source, cond):
            select = _open(_select(source))
            return replace(select, where=(*select.where, cond))
        case Sort(source, keys, reverse):
            select = _open(_select(source))
            return replace(select, order=tuple((k, reverse) for k in keys) + select.order)
        case Slice(source, start, stop):
            return replace(_open(_select(source)), slice=(start, stop))


def _open(select: _Select) -> _Select:
    """`select` if it can still take conditions, an order or a slice, or else a new SELECT on top of it.

    A slice comes last in a SELECT, so whatever comes after a slice works on a SELECT of the slice.
    """
    if select.slice is None:
        return select
    sql, params = _render(select, _columns(select.m))
    # SQL keeps no order from a subquery, so the outer SELECT sorts again.
    return _Select(select.m, f"({sql})", tuple(params), order=select.order)


def _render(select: _Select, columns: str) -> tuple[str, list[object]]:
    sql = f"SELECT {columns} FROM {select.source}"
    params = list(select.source_params)
    if select.where:
        where, where_params = condition_sql(select.where[0] if len(select.where) == 1 else And(select.where))
        sql += f" WHERE {where}"
        params += where_params
    if select.order:
        order = [_column(f) + (" DESC" if descending else "") for f, descending in select.order]
        if not any(f == select.m.key for f, _ in select.order):
            # The key breaks ties, so slices of the same order never repeat or skip a record.
            order.append(quote(select.m.key.name))
        sql += f" ORDER BY {', '.join(order)}"
    if select.slice is not None:
        start, stop = select.slice
        sql += " LIMIT ? OFFSET ?"
        params += [-1 if stop is None else max(stop - start, 0), start]
    return sql, params


def _columns(m: EntityMeta) -> str:
    return ", ".join(quote(f.name) for f in m.fields)
