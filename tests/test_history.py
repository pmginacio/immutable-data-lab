"""How the history db stores versions. Its reads at past transactions are in test_as_of.py."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from helpers import opened

import inlay.history
from inlay import AlreadyExists, Field, Missing, Ref, entity, key
from inlay.history.sql import CURRENT, delete_by_key_sql, select_sql
from inlay.meta import meta
from inlay.nodes import Compare, Filter, Table


@entity
class Customer:
    id: Field[str] = key()
    name: Field[str]


@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]


ANA = Customer(id="ana", name="Ana")
O1 = Order(id="o1", customer="ana", total=10)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shop.db"


@pytest.fixture
def db(path):
    with opened(inlay.history.open, [Customer, Order], path, [ANA, O1]) as db:
        yield db


def versions(path, table):
    """Every stored version of `table`, as (id, tx_from, tx_to), in the order they were written."""
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f'SELECT "id", "_tx_from", "_tx_to" FROM "{table}" ORDER BY rowid').fetchall()


@pytest.mark.parametrize(
    "sql, params",
    [
        select_sql(Filter(Table(Order), Compare(meta(Order).key, "==", "o1")), None),
        (delete_by_key_sql(meta(Order)), ["o1"]),
    ],
    ids=["get", "end a version"],
)
def test_a_current_version_is_found_by_key_without_scanning(db, path, sql, params):
    # Without statistics SQLite could also pick (_tx_to, _tx_from) and walk every current row.
    with closing(sqlite3.connect(path)) as conn:
        plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
    assert any(row.startswith("SEARCH") and "order__id___tx_to" in row for row in plan)


def test_an_update_keeps_the_old_version_and_reads_the_new_one(db, path):
    db.update(replace(O1, total=20))
    assert [o.total for o in db[Order]] == [20]
    assert versions(path, "order") == [("o1", 1, 2), ("o1", 2, CURRENT)]


def test_the_same_key_twice_in_one_update_raises_and_changes_nothing(db):
    # Both records are inserted as current versions, so the unique _live_id refuses the second; inlay keeps the last.
    with pytest.raises(AlreadyExists):
        db.update(replace(O1, total=1), replace(O1, total=2))
    assert [o.total for o in db[Order]] == [10]


@pytest.mark.parametrize(
    "end",
    [
        lambda db: db.remove(O1),
        lambda db: db.discard(O1),
        lambda db: db[Order].delete("o1"),
        lambda db: db[Order].clear(Order.id == "o1"),
    ],
    ids=["remove", "discard", "delete", "clear"],
)
def test_ending_a_record_keeps_its_version_stored(db, path, end):
    end(db)
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


def test_a_removed_key_added_again_is_a_new_version(db, path):
    db.remove(O1)
    db.add(replace(O1, total=30))
    assert versions(path, "order") == [("o1", 1, 2), ("o1", 3, CURRENT)]


def test_two_updates_of_a_record_in_one_transaction_leave_one_visible_version(db, path):
    with db.transaction():
        db.update(replace(O1, total=20))
        db.update(replace(O1, total=30))
    assert [o.total for o in db[Order]] == [30]
    # This pins the stored rows on purpose: the version made first in transaction 2 also ends in it,
    # so no read at any transaction sees it, and only the rows show that it is kept and empty.
    assert versions(path, "order") == [("o1", 1, 2), ("o1", 2, 2), ("o1", 2, CURRENT)]


def test_updating_a_record_that_others_refer_to_leaves_them_alone(db, path):
    db.update(replace(ANA, name="Ana B"))
    assert [o.id for o in db[Order].filter(Order.customer.name == "Ana B")] == ["o1"]
    assert db[Order].filter(Order.customer.name == "Ana").get("o1") is None
    assert versions(path, "order") == [("o1", 1, CURRENT)]


def test_register_and_del_each_take_a_transaction(path):
    with inlay.history.open(path) as db:
        db.register(Customer)
        assert db.tx == 1
        del db[Customer]
        assert db.tx == 2
