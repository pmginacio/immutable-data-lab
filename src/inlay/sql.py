from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from inlay.meta import EntityMeta, FieldMeta, meta
from inlay.nodes import And, BinOp, Call, Compare, CompareOp, Condition, Filter, In, Match, Not, Or, classes_read
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
    m = meta(records.source.cls)
    where, params = condition_sql(records.cond)
    return f"SELECT {quote(m.key.name)} FROM {quote(m.table)} WHERE {where}", params


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


@dataclass(frozen=True, slots=True)
class Query:
    """The records a view picks: conditions, then order, then a slice.

    A slice is applied last, so once a query is sliced, what comes next works on the slice:
    it becomes the `source` of a new query.
    """

    where: And = And(())
    order: tuple[tuple[FieldMeta, bool], ...] = ()  # each field with whether it sorts descending
    limit: int | None = None
    offset: int = 0
    source: "Query | None" = None

    def filter(self, cond: And) -> "Query":
        q = self._open()
        return replace(q, where=And(q.where.items + cond.items))

    def sorted(self, fields: tuple[FieldMeta, ...], reverse: bool) -> "Query":
        # Like sorting twice with the built-in `sorted`: the last sort is the main one, the earlier ones break ties.
        q = self._open()
        return replace(q, order=tuple((f, reverse) for f in fields) + q.order)

    def sliced(self, start: int, stop: int | None) -> "Query":
        q = self._open()
        return replace(q, limit=None if stop is None else max(stop - start, 0), offset=start)

    def reads(self) -> set[type]:
        """The classes that the conditions of this query and of its sources apply to or reach by relations."""
        found = classes_read(self.where)
        if self.source is not None:
            found |= self.source.reads()
        return found

    def _open(self) -> "Query":
        """This query if it can still take conditions, an order or a slice, or else a new query on top of it."""
        if self.limit is None and not self.offset:
            return self
        return Query(order=self.order, source=self)


def select_sql(m: EntityMeta, query: Query) -> tuple[str, list[object]]:
    """A SELECT of every column of the records `query` picks, and its parameters in order."""
    params: list[object] = []
    if query.source is None:
        source = quote(m.table)
    else:
        inner, params = select_sql(m, query.source)
        source = f"({inner})"
    sql = f"SELECT {_columns(m)} FROM {source}"
    if query.where.items:
        where, where_params = condition_sql(query.where)
        sql += f" WHERE {where}"
        params += where_params
    if query.order:
        columns = [_column(f) + (" DESC" if descending else "") for f, descending in query.order]
        if not any(f == m.key for f, _ in query.order):
            # The key breaks ties, so slices of the same order never repeat or skip a record.
            columns.append(quote(m.key.name))
        sql += f" ORDER BY {', '.join(columns)}"
    if query.limit is not None or query.offset:
        sql += " LIMIT ? OFFSET ?"
        params += [-1 if query.limit is None else query.limit, query.offset]
    return sql, params


def aggregate_sql(
    m: EntityMeta, query: Query, function: Aggregate, field: FieldMeta | None = None
) -> tuple[str, list[object]]:
    """One value computed over the records `query` picks: all of them for COUNT, else `field`, a field of `m` itself."""
    inner, params = select_sql(m, query)
    column = "*" if field is None else _column(field)
    return f"SELECT {function}({column}) FROM ({inner})", params


def _columns(m: EntityMeta) -> str:
    return ", ".join(quote(f.name) for f in m.fields)
