from inlay.entity import EntityMeta, FieldMeta, meta
from inlay.types import SQL_TYPES

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


def _columns(m: EntityMeta) -> str:
    return ", ".join(quote(f.column) for f in m.fields)
