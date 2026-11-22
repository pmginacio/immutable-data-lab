"""Enum fields: a `StrEnum` or an `IntEnum`, stored as its value and read back as a member."""

import sqlite3
from enum import Enum, IntEnum, StrEnum

import pytest

import inlay
from inlay import entity, hashed, key


class Kind(StrEnum):
    FLAT = "flat"
    HOUSE = "house"


class Floor(IntEnum):
    GROUND = 0
    FIRST = 1


@entity
class Home:
    id: str = key()
    kind: Kind
    floor: Floor | None = None


@entity
class Look:
    id: str = hashed()
    kind: Kind


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "homes.db") as db:
        db.register(Home)
        db.add(Home(id="h1", kind=Kind.HOUSE, floor=None), Home(id="h2", kind=Kind.FLAT, floor=Floor.FIRST))
        yield db


def test_a_member_is_stored_as_its_value_and_read_back_as_a_member(db, tmp_path):
    h2 = db[Home].get("h2")
    assert h2 is not None and (h2.kind, h2.floor) == (Kind.FLAT, Floor.FIRST)
    assert type(h2.kind) is Kind
    rows = sqlite3.connect(tmp_path / "homes.db").execute("SELECT kind, floor FROM home ORDER BY id").fetchall()
    assert rows == [("house", None), ("flat", 1)]


def test_conditions_and_groups_use_members(db):
    assert [h.id for h in db[Home].filter(Home.kind == Kind.HOUSE)] == ["h1"]
    assert dict(db[Home].aggregate_by(Home.kind, len).items()) == {Kind.FLAT: 1, Kind.HOUSE: 1}


def test_a_hashed_key_is_the_same_as_for_the_plain_value():
    @entity
    class PlainLook:  # the fields of Look as they were before `kind` became an enum
        id: str = hashed()
        kind: str

    assert PlainLook(kind="flat").id == Look(kind=Kind.FLAT).id


def _plain_home() -> type:
    @entity
    class Home:  # Home as it was before its fields became enums, with the same table
        id: str = key()
        kind: str
        floor: int | None = None

    return Home


def test_a_str_field_that_becomes_a_str_enum_keeps_its_table(tmp_path):
    plain = _plain_home()
    with inlay.open(tmp_path / "homes.db") as db:
        db.register(plain)
        db.add(plain(id="h1", kind="house"))
    with inlay.open(tmp_path / "homes.db") as db:
        assert [h.kind for h in db[Home]] == [Kind.HOUSE]


def test_a_stored_value_that_is_no_member_raises(db, tmp_path):
    db.close()
    with sqlite3.connect(tmp_path / "homes.db") as conn:
        conn.execute("UPDATE home SET kind = 'villa' WHERE id = 'h1'")
    with inlay.open(tmp_path / "homes.db") as db, pytest.raises(ValueError, match="'villa' is not a valid Kind"):
        list(db[Home])


def test_other_enums_are_refused():
    class Color(Enum):
        RED = 1

    with pytest.raises(TypeError, match="unsupported type"):
        @entity
        class Paint:
            id: str = key()
            color: Color
