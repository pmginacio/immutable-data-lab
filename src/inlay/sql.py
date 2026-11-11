from collections.abc import Sequence

from inlay.conditions import And, Compare, Condition, FieldRef, Match, Not, OneOf, Or
from inlay.entity import EntityMeta, FieldMeta, meta
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


def create_table_sql(m: EntityMeta) -> str:
    """Written exactly the way SQLite stores it, so it can be compared with an existing table."""
    return f"CREATE TABLE {quote(m.table)} ({', '.join(map(_column_def, m.fields))}) STRICT"


def create_index_sql(m: EntityMeta, index: tuple[FieldMeta, ...]) -> str:
    name = "__".join((m.table, *(f.name for f in index)))
    columns = ", ".join(quote(f.column) for f in index)
    return f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {quote(m.table)} ({columns})"


def _column_def(f: FieldMeta) -> str:
    sql = f"{quote(f.column)} {SQL_TYPES[f.py_type]} NOT NULL"
    if f.is_key:
        sql += " PRIMARY KEY"
    if f.target is not None:
        target = meta(f.target)
        # Checked at commit, so the order of writes inside a transaction does not matter.
        sql += f" REFERENCES {quote(target.table)} ({quote(target.key.column)}) DEFERRABLE INITIALLY DEFERRED"
    return sql


def select_by_key_sql(m: EntityMeta) -> str:
    return f"SELECT {_columns(m)} FROM {quote(m.table)} WHERE {quote(m.key.column)} = ?"


def insert_sql(m: EntityMeta) -> str:
    return f"INSERT INTO {quote(m.table)} ({_columns(m)}) VALUES ({', '.join('?' * len(m.fields))})"


def upsert_sql(m: EntityMeta) -> str:
    updates = ", ".join(f"{quote(f.column)} = excluded.{quote(f.column)}" for f in m.fields if not f.is_key)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return f"{insert_sql(m)} ON CONFLICT ({quote(m.key.column)}) {action}"


def delete_by_key_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)} WHERE {quote(m.key.column)} = ?"


def delete_equal_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)} WHERE {' AND '.join(f'{quote(f.column)} = ?' for f in m.fields)}"


def clear_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)}"


_COMPARE_OPS = {"==": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def condition_sql(m: EntityMeta, cond: Condition) -> tuple[str, list[object]]:
    """The SQL for `cond` on the table of `m`, and its parameters in order."""
    match cond:
        case Compare(left, op, FieldRef() as right):
            if (right.owner, right.via) != (left.owner, left.via):
                raise ValueError("only two fields of the same record can be compared")
            return _on_path(m, left, f"{_column(left)} {_COMPARE_OPS[op]} {_column(right)}", [])
        case Compare(left, op, value):
            return _on_path(m, left, f"{_column(left)} {_COMPARE_OPS[op]} ?", [to_sql(value)])
        case Match(field, "startswith", text):
            return _on_path(m, field, f"substr({_column(field)}, 1, ?) = ?", [len(text), text])
        case Match(field, "endswith", text):
            column = _column(field)
            return _on_path(m, field, f"substr({column}, length({column}) - ? + 1) = ?", [len(text), text])
        case Match(field, "contains", text):
            return _on_path(m, field, f"instr({_column(field)}, ?) > 0", [text])
        case OneOf(field, values):
            marks = ", ".join("?" * len(values))
            return _on_path(m, field, f"{_column(field)} IN ({marks})", [to_sql(v) for v in values])
        case And(items):
            return _join(m, items, " AND ", "1")
        case Or(items):
            return _join(m, items, " OR ", "0")
        case Not(item):
            sql, params = condition_sql(m, item)
            return f"NOT ({sql})", params
        case _:
            raise TypeError(f"expected a condition, got {cond!r}")


def _join(m: EntityMeta, items: Sequence[Condition], operator: str, empty: str) -> tuple[str, list[object]]:
    parts = [condition_sql(m, item) for item in items]
    if not parts:
        return empty, []
    return f"({operator.join(sql for sql, _ in parts)})", [p for _, params in parts for p in params]


def _column(ref: FieldRef) -> str:
    return quote(meta(ref.owner).field(ref.name).column)


def _on_path(m: EntityMeta, ref: FieldRef, sql: str, params: list[object]) -> tuple[str, list[object]]:
    """Wraps `sql`, a test on `ref`'s own table, in one subquery for each relation between `m` and that table."""
    start = ref.via[0][0] if ref.via else ref.owner
    if start is not m.cls:
        raise ValueError(f"{ref.owner.__name__}.{ref.name} cannot be used on {m.cls.__name__}")
    for owner, name in reversed(ref.via):
        f = meta(owner).field(name)
        assert f.target is not None
        target = meta(f.target)
        sql = f"{quote(f.column)} IN (SELECT {quote(target.key.column)} FROM {quote(target.table)} WHERE {sql})"
    return sql, params


def _columns(m: EntityMeta) -> str:
    return ", ".join(quote(f.column) for f in m.fields)
