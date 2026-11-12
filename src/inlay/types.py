from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

SQL_TYPES: dict[type, str] = {
    int: "INTEGER",
    float: "REAL",
    str: "TEXT",
    bytes: "BLOB",
    bool: "INTEGER",
    datetime: "TEXT",
    date: "TEXT",
}

_FROM_SQL: dict[type, Callable[[Any], object]] = {
    bool: bool,
    datetime: datetime.fromisoformat,
    date: date.fromisoformat,
}


def to_sql(value: object) -> object:
    """`datetime` and `date` are stored as ISO 8601 text; `sqlite3` handles the other types."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        # SQLite compares the text, which only follows time order when every value has the same UTC offset.
        value = value.astimezone(UTC)
    return value.isoformat() if isinstance(value, date) else value


def from_sql(value: object, py_type: type) -> object:
    convert = _FROM_SQL.get(py_type)
    return value if convert is None else convert(value)
