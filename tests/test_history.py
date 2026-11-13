import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

import inlay.history
from inlay import AlreadyExists, BrokenRef, Missing, Ref, StoreError, entity, key

CURRENT = 2**63 - 1


@entity
class Customer:
    id: str = key()
    name: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


ANA = Customer(id="ana", name="Ana")
O1 = Order(id="o1", customer="ana", total=10)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shop.db"


@pytest.fixture
def db(path):
    with inlay.history.open([Customer, Order], path=path) as db:
        db.add(ANA, O1)
        yield db


def versions(path, table):
    """Every stored version of `table`, as (id, tx_from, tx_to), in the order they were written."""
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f'SELECT "id", "tx_from", "tx_to" FROM "{table}" ORDER BY rowid').fetchall()


def transactions(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute('SELECT "tx", "at" FROM "inlay_tx" ORDER BY "tx"').fetchall()


def test_an_update_keeps_the_old_version_and_reads_the_new_one(db, path):
    db.update(replace(O1, total=20))
    assert [o.total for o in db[Order]] == [20]
    assert versions(path, "order") == [("o1", 1, 2), ("o1", 2, CURRENT)]


def test_update_adds_a_record_that_is_not_stored(db):
    db.update(Order(id="o2", customer="ana", total=5))
    assert {o.id for o in db[Order]} == {"o1", "o2"}


def test_the_same_key_twice_in_one_update_ends_with_the_last_record(db):
    db.update(replace(O1, total=1), replace(O1, total=2))
    assert [o.total for o in db[Order]] == [2]


def test_adding_a_key_that_is_current_raises(db):
    with pytest.raises(AlreadyExists):
        db.add(replace(O1, total=99))


def test_removing_ends_the_current_version_and_keeps_it_stored(db, path):
    db.remove(O1)
    assert len(db[Order]) == 0
    assert versions(path, "order") == [("o1", 1, 2)]


@pytest.mark.parametrize(
    "write",
    [lambda db: db.remove(replace(O1, total=99)), lambda db: db[Order].delete("o1", "nope")],
)
def test_ending_a_record_that_is_not_current_raises_and_changes_nothing(db, path, write):
    with pytest.raises(Missing):
        write(db)
    assert versions(path, "order") == [("o1", 1, CURRENT)]


def test_discard_and_delete_and_clear_end_current_versions(db):
    db.add(Order(id="o2", customer="ana", total=5), Order(id="o3", customer="ana", total=50))
    db.discard(replace(O1, total=99))  # not stored, so nothing happens
    db[Order].delete("o1")
    db[Order].clear(Order.total < 10)
    assert [o.id for o in db[Order]] == ["o3"]


def test_a_removed_key_can_be_added_again(db, path):
    db.remove(O1)
    db.add(replace(O1, total=30))
    assert [o.total for o in db[Order]] == [30]
    assert versions(path, "order") == [("o1", 1, 2), ("o1", 3, CURRENT)]


def test_two_updates_of_a_record_in_one_transaction_leave_one_visible_version(db, path):
    with db.transaction():
        db.update(replace(O1, total=20))
        db.update(replace(O1, total=30))
    assert [o.total for o in db[Order]] == [30]
    # The version made first in transaction 2 also ends in it, so no read at any transaction sees it.
    assert versions(path, "order") == [("o1", 1, 2), ("o1", 2, 2), ("o1", 2, CURRENT)]


def test_a_record_that_a_current_record_refers_to_cannot_be_removed(db):
    with pytest.raises(BrokenRef):
        db.remove(ANA)
    db.remove(O1, ANA)
    assert len(db[Customer]) == 0


def test_a_reference_to_a_removed_record_raises(db):
    db.remove(O1, ANA)
    with pytest.raises(BrokenRef):
        db.add(Order(id="o2", customer="ana", total=5))


def test_updating_a_record_that_others_refer_to_leaves_them_alone(db, path):
    db.update(replace(ANA, name="Ana B"))
    assert [o.id for o in db[Order].filter(Order.customer.name == "Ana B")] == ["o1"]
    assert db[Order].filter(Order.customer.name == "Ana").get("o1") is None
    assert versions(path, "order") == [("o1", 1, CURRENT)]


def test_each_write_takes_the_next_transaction_and_times_never_go_back(db, path):
    future = "2999-01-01T00:00:00+00:00"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('UPDATE "inlay_tx" SET "at" = ?', (future,))
    db.update(replace(O1, total=20))
    assert transactions(path) == [(1, future), (2, future)]


def test_no_transaction_takes_the_number_that_marks_a_current_version(db, path):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('INSERT INTO "inlay_tx" VALUES (?, ?)', (CURRENT - 1, "2026-01-01T00:00:00+00:00"))
    with pytest.raises(StoreError, match="every transaction number"):
        db.update(replace(O1, total=20))
    assert versions(path, "order") == [("o1", 1, CURRENT)]
