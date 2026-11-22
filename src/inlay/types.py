from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import Enum, IntEnum, StrEnum
from typing import Any

_SQL_TYPES: dict[type, str] = {
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
    float: float,  # a column without a type keeps an int stored for a float field
    datetime: datetime.fromisoformat,
    date: date.fromisoformat,
}


def sql_type(py_type: Any) -> str | None:
    """The column type that stores `py_type`, or None if it cannot be stored.

    A `StrEnum` or an `IntEnum` is stored as its value, so it has the column of a `str` or an `int`.
    """
    if isinstance(py_type, type) and issubclass(py_type, StrEnum | IntEnum):
        py_type = str if issubclass(py_type, StrEnum) else int
    return _SQL_TYPES.get(py_type)


def to_sql(value: object) -> object:
    """`datetime` and `date` are stored as ISO 8601 text, an enum as its value; `sqlite3` handles the other types."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime) and value.tzinfo is not None:
        # SQLite compares the text, which only follows time order when every value has the same UTC offset.
        value = value.astimezone(UTC)
    return value.isoformat() if isinstance(value, date) else value


def from_sql(value: object, py_type: type) -> object:
    """`value` as stored, read back as `py_type`; a stored value that is not a member of an enum raises."""
    convert = py_type if issubclass(py_type, Enum) else _FROM_SQL.get(py_type)
    return value if convert is None or value is None else convert(value)
