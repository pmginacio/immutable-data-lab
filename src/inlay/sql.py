from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from inlay.functions import Aggregate
from inlay.meta import EntityMeta, FieldMeta, meta
from inlay.nodes import (
    Aggregated,
    And,
    BinOp,
    Call,
    Compare,
    CompareOp,
    Condition,
    GROUP_KEY,
    GROUP_VALUE,
    Filter,
    Group,
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
)
from inlay.types import sql_type, to_sql

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


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


ROOT = "0"  # the name of the records of a SELECT; a path is named after identifiers, which never start with a digit


@dataclass(frozen=True)
class _Rows:
    """Where the columns of a statement come from: its records, named `root`, and the row of each path of `Ref`s
    joined, named after the path, as "ad.state".

    With no `root`, as in a DELETE, there is one table and columns need no name.
    """

    root: str | None = None
    joined: Mapping[tuple[FieldMeta, ...], str] = field(default_factory=dict)

    def column(self, field: FieldMeta) -> str:
        return _column(field) if self.root is None else f"{quote(self.root)}.{_column(field)}"


_ONE_TABLE = _Rows()


def create_table_sql(m: EntityMeta, table: str | None = None) -> str:
    """The CREATE TABLE statement for `m`.

    SQLite keeps this text, and `open` compares it with what this function returns now,
    so any change to it, even in spacing, makes existing files fail the schema check.

    `table` is the name to use instead of the table of `m`, to compare a table with the shape of another class.
    References of `m` to itself use that name too, since they point to the same table.
    """
    table = table or m.table
    return f"CREATE TABLE {quote(table)} ({', '.join(_column_def(f, table) for f in m.fields)}) STRICT"


def index_prefix(table: str) -> str:
    """How the name of every index inlay makes for `table` starts, so the ones made by hand can be told apart."""
    return f"{table}__"


def indexes_sql(m: EntityMeta) -> dict[str, str]:
    """The CREATE INDEX statement of every index `m` declares, by name: the prefix and the names of its fields."""
    statements: dict[str, str] = {}
    for index in m.indexes:
        name = index_prefix(m.table) + "__".join(f.name for f in index)
        columns = ", ".join(map(_column, index))
        statements[name] = f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {quote(m.table)} ({columns})"
    return statements


def drop_index_sql(name: str) -> str:
    return f"DROP INDEX {quote(name)}"


def rename_table_sql(old: str, new: str) -> str:
    return f"ALTER TABLE {quote(old)} RENAME TO {quote(new)}"


def drop_table_sql(table: str) -> str:
    return f"DROP TABLE {quote(table)}"


def reprefix_index_sql(name: str, sql: str, old_table: str, new_table: str) -> tuple[str, str]:
    """Drop an index of a renamed table and create it again, named after the new table.

    `sql` is the stored text, which already says `ON` the new table.
    """
    new_name = index_prefix(new_table) + name.removeprefix(index_prefix(old_table))
    return f"DROP INDEX {quote(name)}", sql.replace(quote(name), quote(new_name), 1)


def copy_rows_sql(table: str, m: EntityMeta) -> str:
    """Copy every row of `table`, which has the columns of `m`, into the table of `m`."""
    return f"INSERT INTO {quote(m.table)} ({_columns(m)}) SELECT {_columns(m)} FROM {quote(table)}"


def _column_def(f: FieldMeta, table: str) -> str:
    sql = f"{_column(f)} {sql_type(f.py_type)}" + ("" if f.optional else " NOT NULL")
    if f.is_key:
        sql += " PRIMARY KEY"
    if f.target is not None:
        target = meta(f.target)
        target_table = table if f.target is f.owner else target.table
        # Checked at commit, so the order of writes inside a transaction does not matter.
        sql += f" REFERENCES {quote(target_table)} ({_column(target.key)}) DEFERRABLE INITIALLY DEFERRED"
    return sql


def insert_sql(m: EntityMeta) -> str:
    return f"INSERT INTO {quote(m.table)} ({_columns(m)}) VALUES ({', '.join('?' * len(m.fields))})"


def upsert_sql(m: EntityMeta) -> str:
    updates = ", ".join(f"{_column(f)} = excluded.{_column(f)}" for f in m.fields if not f.is_key)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return f"{insert_sql(m)} ON CONFLICT ({_column(m.key)}) {action}"


def delete_by_key_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)} WHERE {_column(m.key)} = ?"


def delete_equal_sql(m: EntityMeta) -> str:
    return f"DELETE FROM {quote(m.table)} WHERE {' AND '.join(f'{_column(f)} IS ?' for f in m.fields)}"


def clear_sql(m: EntityMeta, cond: Condition) -> tuple[str, list[object]]:
    """A DELETE of every record that matches `cond`, and its parameters."""
    where, params = condition_sql(cond)
    return f"DELETE FROM {quote(m.table)} WHERE {where}", params


# SQLite's IS and IS NOT compare like = and !=, with the same use of indexes, but also match a NULL as Python does None.
_SQL_OPS: dict[CompareOp, str] = {"==": "IS", "!=": "IS NOT"}


def _sql_op(op: CompareOp) -> str:
    return _SQL_OPS.get(op, op)


def condition_sql(cond: Condition, rows: _Rows = _ONE_TABLE) -> tuple[str, list[object]]:
    """The SQL for `cond` on `rows`, and its parameters in order. `cond` was checked by `check_condition`."""
    match cond:
        case Compare(left, op, right):
            left_sql, left_params = value_sql(left, rows)
            right_sql, right_params = value_sql(right, rows)
            return f"{left_sql} {_sql_op(op)} {right_sql}", left_params + right_params
        case Match(field, "startswith", text):
            return f"substr({rows.column(field)}, 1, ?) = ?", [len(text), text]
        case Match(field, "endswith", text):
            column = rows.column(field)
            return f"substr({column}, length({column}) - ? + 1) = ?", [len(text), text]
        case Match(field, "contains", text):
            return f"instr({rows.column(field)}, ?) > 0", [text]
        case In(field, Filter() as records):
            keys, params = _keys_sql(records)
            return f"{rows.column(field)} IN ({keys})", params
        case In(field, tuple() as values):
            given = [to_sql(v) for v in values if v is not None]
            column = rows.column(field)
            sql = f"{column} IN ({', '.join('?' * len(given))})"
            # IN never matches a NULL, even with NULL among the values.
            return (f"({sql} OR {column} IS NULL)" if None in values else sql), given
        case And(items):
            return _join(items, " AND ", "1", rows)
        case Or(items):
            return _join(items, " OR ", "0", rows)
        case Not(item):
            sql, params = condition_sql(item, rows)
            # A comparison with a NULL is neither true nor false in SQL, and so is its NOT;
            # counted as false first, its negation is true, as for any other record that does not match.
            return f"NOT coalesce(({sql}), 0)", params
        case _:
            raise TypeError(f"expected a condition, got {cond!r}")


def _join(items: Sequence[Condition], operator: str, empty: str, rows: _Rows) -> tuple[str, list[object]]:
    parts = [condition_sql(item, rows) for item in items]
    if not parts:
        return empty, []
    return f"({operator.join(sql for sql, _ in parts)})", [p for _, params in parts for p in params]


def _keys_sql(records: Filter) -> tuple[str, list[object]]:
    """A SELECT of the keys of `records`, and its parameters."""
    select = _select(records)
    return _render(select, [(select.m.key.name, select.m.key)])


def value_sql(value: object, rows: _Rows = _ONE_TABLE) -> tuple[str, list[object]]:
    """The SQL for a `Value` on `rows`, or a parameter for a plain value, and its parameters in order."""
    match value:
        case FieldMeta():
            return rows.column(value), []
        case Call(fn, args):
            parts = [value_sql(v, rows) for v in args]
            return fn.sql_of([sql for sql, _ in parts]), [p for _, params in parts for p in params]
        case BinOp(op, left, right):
            left_sql, left_params = value_sql(left, rows)
            right_sql, right_params = value_sql(right, rows)
            if op == "/":
                # SQLite divides two integers without a remainder; Python's `/` does not.
                left_sql = f"CAST({left_sql} AS REAL)"
            return f"({left_sql} {op} {right_sql})", left_params + right_params
        case Aggregated(aggregate, inner, ref, cond) if ref is not None and rows.root is not None:
            return _aggregated_sql(aggregate, inner, ref, cond, rows)
        case Aggregated():
            raise TypeError("an aggregate such as Order.amount.sum() can only be the value of a projection field")
        case RefValue(refs, inner) if refs in rows.joined:
            return value_sql(inner, _Rows(rows.joined[refs]))
        case RefValue(refs, inner):
            return _ref_value_sql(refs, inner, 0)
        case GroupValue(groups, at):
            grouped, grouped_params = select_sql(groups)
            parts = [value_sql(v, rows) for v in at]
            # The columns of `grouped` start with _inlay_, so the fields `at` names are read from the record at hand.
            match = " AND ".join(f"{quote(GROUP_KEY.format(i))} IS {sql}" for i, (sql, _) in enumerate(parts))
            sql = f"(SELECT {quote(GROUP_VALUE)} FROM ({grouped}) WHERE {match})"
            return sql, grouped_params + [p for _, params in parts for p in params]
        case _:
            return "?", [to_sql(value)]


def _aggregated_sql(
    aggregate: Aggregate[Any],
    value: Value,
    ref: FieldMeta,
    cond: Condition | None,
    rows: _Rows,
) -> tuple[str, list[object]]:
    """`aggregate` of `value` over the records whose `ref` points at the record of `rows`, those `cond` keeps:
    a subquery on `ref`'s column, which an index always leads.

    Its records are named after the outer ones and `ref`, as "0/customer", so the outer record stays reachable."""
    root = f"{rows.root}/{ref.name}"
    inner = _Rows(root, _joined_paths(_ref_paths([value, cond] if cond is not None else [value])))
    value_part, params = value_sql(value, inner)
    assert ref.target is not None
    where = f"{inner.column(ref)} = {rows.column(meta(ref.target).key)}"
    if cond is not None:
        cond_sql, cond_params = condition_sql(cond, inner)
        where += f" AND {cond_sql}"
        params = params + cond_params
    sql = f"(SELECT {aggregate.sql.format(value_part)} FROM {quote(meta(ref.owner).table)} AS {quote(root)}"
    return f"{sql}{_joins_sql(inner)} WHERE {where})", params


def _ref_value_sql(refs: tuple[FieldMeta, ...], value: Value, outer: int) -> tuple[str, list[object]]:
    """`value` read through `refs` from the row named by `outer`, with one subquery for each `Ref` followed.

    Each table gets its own name, so the `Ref` column is always read from the outer row,
    even when both tables are of the same class.
    """
    if not refs:
        return value_sql(value)
    ref, *rest = refs
    assert ref.target is not None
    target = meta(ref.target)
    sql, params = _ref_value_sql(tuple(rest), value, outer + 1)
    row, outer_row = _row(outer + 1), _row(outer)
    return (
        f"(SELECT {sql} FROM {quote(target.table)} AS {row} WHERE {_column(target.key)} = {outer_row}.{_column(ref)})",
        params,
    )


def _row(depth: int) -> str:
    """The name of the row at `depth`: 0 for the records of a SELECT, and one more for each `Ref` followed from them."""
    return quote(f"_{depth}")


def _column(field: FieldMeta) -> str:
    return quote(field.name)


def select_sql(query: Node) -> tuple[str, list[object]]:
    """A SELECT of every column of the records `query` picks, and its parameters in order."""
    select = _select(query)
    return _render(select, [(f.name, f) for f in select.m.fields])


def aggregate_sql(
    query: Node, aggregate: Aggregate[Any], value: Value | None = None, keys: tuple[Value, ...] = ()
) -> tuple[str, list[object]]:
    """`aggregate` over the records `query` picks, of `value` if it takes one.

    With `keys`, one row for each distinct key: the values of `keys`, then the aggregate.
    """
    inner, inner_params = select_sql(query)
    rows = _Rows(ROOT, _joined_paths(_ref_paths([*keys, value])))
    parts = [value_sql(k, rows) for k in keys]
    value_part, value_params = ("", []) if value is None else value_sql(value, rows)
    columns = [f"{sql} AS {quote(GROUP_KEY.format(i))}" for i, (sql, _) in enumerate(parts)]
    columns.append(f"{aggregate.sql.format(value_part)} AS {quote(GROUP_VALUE)}")
    sql = f"SELECT {', '.join(columns)} FROM ({inner}) AS {quote(ROOT)}{_joins_sql(rows)}"
    if keys:
        # Numbers stand for the columns of the result, so each key and its parameters appear only once.
        sql += f" GROUP BY {', '.join(str(i) for i in range(1, len(keys) + 1))}"
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


def _select(query: Node) -> _Select:
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
        case Project(source, projection):
            # The rows of the root with a column for each field, so whatever comes after reads them as a table.
            columns = [(f.name, v) for f, v in zip(projection.m.fields, projection.values, strict=True)]
            sql, params = _render(_select(source), columns)
            return _Select(projection.m, f"({sql})", tuple(params))
        case Group(source, aggregate, value, keys, m):
            sql, params = aggregate_sql(source, aggregate, value, keys)
            # In key order unless sorted otherwise, and by key after any other order.
            return _Select(m, f"({sql})", tuple(params), order=tuple((f, False) for f in m.fields if f.is_key))


def _open(select: _Select) -> _Select:
    """`select` if it can still take conditions, an order or a slice, or else a new SELECT on top of it.

    A slice comes last in a SELECT, so whatever comes after a slice works on a SELECT of the slice.
    """
    if select.slice is None:
        return select
    sql, params = _render(select, [(f.name, f) for f in select.m.fields])
    # SQL keeps no order from a subquery, so the outer SELECT sorts again.
    return _Select(select.m, f"({sql})", tuple(params), order=select.order)


def _render(select: _Select, columns: Sequence[tuple[str, Value]]) -> tuple[str, list[object]]:
    """The SELECT of `columns`, each a name and its value, from the records of `select`.

    Each path of `Ref`s the values, conditions and sort keys read through is joined once, so fields read through
    the same `Ref`s share one row; a `Ref` points at a key, so a join matches at most one row and none multiply.
    """
    where = select.where[0] if len(select.where) == 1 else And(select.where)
    rows = _Rows(ROOT, _joined_paths(_ref_paths([*(v for _, v in columns), *(k for k, _ in select.order), where])))
    parts = [value_sql(v, rows) for _, v in columns]
    sql = f"SELECT {', '.join(f'{part} AS {quote(name)}' for (part, _), (name, _) in zip(parts, columns))}"
    sql += f" FROM {select.source} AS {quote(ROOT)}{_joins_sql(rows)}"
    params = [p for _, ps in parts for p in ps] + list(select.source_params)
    if select.where:
        where_sql, where_params = condition_sql(where, rows)
        sql += f" WHERE {where_sql}"
        params += where_params
    if select.order:
        order = []
        for key, descending in select.order:
            key_sql, key_params = value_sql(key, rows)
            order.append(key_sql + (" DESC" if descending else ""))
            params += key_params
        if not any(key == select.m.key for key, _ in select.order):
            # The key breaks ties, so slices of the same order never repeat or skip a record.
            order.append(rows.column(select.m.key))
        sql += f" ORDER BY {', '.join(order)}"
    if select.slice is not None:
        start, stop = select.slice
        sql += " LIMIT ? OFFSET ?"
        params += [-1 if stop is None else max(stop - start, 0), start]
    return sql, params


def _ref_paths(nodes: Iterable[object]) -> Iterator[tuple[FieldMeta, ...]]:
    """The paths of `Ref`s that values in `nodes`, values or conditions, read through from the record at hand."""
    for node in nodes:
        match node:
            case RefValue(refs, _):
                yield refs
            case Call(_, args):
                yield from _ref_paths(args)
            case BinOp(_, left, right) | Compare(left, _, right):
                yield from _ref_paths((left, right))
            case GroupValue(at=at):
                yield from _ref_paths(at)
            case And(items) | Or(items):
                yield from _ref_paths(items)
            case Not(item):
                yield from _ref_paths((item,))


def _joined_paths(paths: Iterable[tuple[FieldMeta, ...]]) -> dict[tuple[FieldMeta, ...], str]:
    """A row for each path, and for each step along it, since a step joins through the one before; named after
    the path, as "ad.state"."""
    joined: dict[tuple[FieldMeta, ...], str] = {}
    for path in paths:
        for end in range(1, len(path) + 1):
            if path[:end] not in joined:
                joined[path[:end]] = ".".join(ref.name for ref in path[:end])
    return joined


def _joins_sql(rows: _Rows) -> str:
    """A LEFT JOIN of each path's row, on the key its last `Ref` holds, so an empty `Ref` gives NULLs."""
    joins = []
    for path, name in rows.joined.items():
        ref = path[-1]
        assert ref.target is not None
        target = meta(ref.target)
        parent = rows.joined.get(path[:-1], rows.root)
        assert parent is not None
        joins.append(
            f" LEFT JOIN {quote(target.table)} AS {quote(name)}"
            f" ON {quote(name)}.{_column(target.key)} = {quote(parent)}.{_column(ref)}"
        )
    return "".join(joins)


def _columns(m: EntityMeta) -> str:
    return ", ".join(map(_column, m.fields))
