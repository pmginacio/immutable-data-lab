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
LIST_INDEXES = "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL"
REFERRERS = (
    "SELECT DISTINCT m.name FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS f "
    "WHERE m.type = 'table' AND f.\"table\" = ?"
)
# Foreign keys are not checked while tables are swapped, and renaming a table leaves the references to it alone.
MIGRATION_SETUP = ("PRAGMA foreign_keys = OFF", "PRAGMA legacy_alter_table = ON")
FOREIGN_KEY_CHECK = "PRAGMA foreign_key_check"


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def create_table_sql(m: EntityMeta, table: str | None = None) -> str:
    """The CREATE TABLE statement for `m`.

    SQLite keeps this text, and `open` compares it with what this function returns now,
    so any change to it, even in spacing, makes existing files fail the schema check.

    `table` is the name to use instead of the table of `m`, to compare a table with the shape of another class.
    References of `m` to itself use that name too, since they point to the same table.
    """
    table = table or m.table
    return f"CREATE TABLE {quote(table)} ({', '.join(_column_def(f, table) for f in m.fields)}) STRICT"


def create_index_sql(m: EntityMeta, index: tuple[FieldMeta, ...]) -> str:
    name = "__".join((m.table, *(f.name for f in index)))
    columns = ", ".join(quote(f.name) for f in index)
    return f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {quote(m.table)} ({columns})"


def rename_table_sql(old: str, new: str) -> str:
    return f"ALTER TABLE {quote(old)} RENAME TO {quote(new)}"


def drop_table_sql(table: str) -> str:
    return f"DROP TABLE {quote(table)}"


def reprefix_index_sql(name: str, sql: str, old_table: str, new_table: str) -> tuple[str, str]:
    """Drop an index of a renamed table and create it again, named after the new table.

    `sql` is the stored text, which already says `ON` the new table.
    """
    new_name = new_table + name.removeprefix(old_table)
    return f"DROP INDEX {quote(name)}", sql.replace(quote(name), quote(new_name), 1)


def repoint_sql(table: str, stored_sql: str, old: EntityMeta, new: EntityMeta) -> tuple[str, ...]:
    """Rebuild `table` so that its foreign keys to `old` point to `new`, keeping its rows.

    `stored_sql` is the stored `CREATE TABLE` of `table`; it names its references in the format of `_column_def`.
    """
    reference = f"REFERENCES {quote(old.table)} ({quote(old.key.name)})"
    if reference not in stored_sql:
        raise ValueError(f"table {table!r} has no foreign key to {old.table!r}")
    staging = f"{table}__rebuild"
    prefix = f"CREATE TABLE {quote(table)} "
    new_reference = f"REFERENCES {quote(new.table)} ({quote(new.key.name)})"
    columns = stored_sql.removeprefix(prefix).replace(reference, new_reference)
    return (
        f"CREATE TABLE {quote(staging)} {columns}",
        f"INSERT INTO {quote(staging)} SELECT * FROM {quote(table)}",
        drop_table_sql(table),
        rename_table_sql(staging, table),
    )


def _column_def(f: FieldMeta, table: str) -> str:
    sql = f"{quote(f.name)} {SQL_TYPES[f.py_type]} NOT NULL"
    if f.is_key:
        sql += " PRIMARY KEY"
    if f.target is not None:
        target = meta(f.target)
        target_table = table if f.target is f.owner else target.table
        # Checked at commit, so the order of writes inside a transaction does not matter.
        sql += f" REFERENCES {quote(target_table)} ({quote(target.key.name)}) DEFERRABLE INITIALLY DEFERRED"
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


def aggregate_sql(
    query: Query, aggregate: Aggregate[Any], value: Value | None = None, keys: tuple[Value, ...] = ()
) -> tuple[str, list[object]]:
    """`aggregate` over the records `query` picks, of `value` if it takes one.

    With `keys`, one row for each distinct key, sorted by key: the values of `keys`, then the aggregate.
    """
    inner, inner_params = select_sql(query)
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
    """The parts of one SELECT, which takes its records from `source`: a table, or a query in parentheses."""

    m: EntityMeta
    source: str
    source_params: tuple[object, ...] = ()
    where: tuple[Condition, ...] = ()
    order: tuple[tuple[Value, bool], ...] = ()  # each key with whether it sorts descending
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
        order = []
        for key, descending in select.order:
            key_sql, key_params = value_sql(key)
            order.append(key_sql + (" DESC" if descending else ""))
            params += key_params
        if not any(key == select.m.key for key, _ in select.order):
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
