"""How the datoms version stores datoms. Its reads at past transactions are in test_as_of.py."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

import inlay.datoms
from inlay import Missing, Ref, entity, key
from inlay.datoms.sql import delete_by_key_sql, select_sql
from inlay.meta import meta
from inlay.nodes import Compare, Filter, Table


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
O1_DATOMS = [("o1", "customer", "ana", 1, 1), ("o1", "id", "o1", 1, 1), ("o1", "total", 10, 1, 1)]


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shop.db"


@pytest.fixture
def db(path):
    with inlay.datoms.open([Customer, Order], path=path) as db:
        db.add(ANA, O1)
        yield db


def datoms(path, cls):
    """Every stored datom of class `cls`, as (e, a, v, tx, added), by record, field and transaction."""
    with closing(sqlite3.connect(path)) as conn:
        sql = 'SELECT e, a, v, tx, added FROM "_inlay_datoms" WHERE class = ? ORDER BY e, a, tx'
        return conn.execute(sql, (meta(cls).table,)).fetchall()


@pytest.mark.parametrize(
    "sql, params",
    [
        select_sql(Filter(Table(Order), Compare(meta(Order).key, "==", "o1")), None),
        select_sql(Filter(Table(Order), Compare(meta(Order).key, "==", "o1")), 1),
        (delete_by_key_sql(meta(Order)), ["o1"]),
    ],
    ids=["get", "get at a transaction", "delete"],
)
def test_a_record_is_found_by_key_without_rebuilding_its_class(db, path, sql, params):
    # SQLite pushes a condition on the key through one grouping only; through two it rebuilds every record.
    with closing(sqlite3.connect(path)) as conn:
        plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
    assert any(row.startswith("SEARCH d ") and "(class=? AND e=?)" in row for row in plan)


def test_an_update_adds_a_datom_only_for_the_fields_that_change(db, path):
    db.update(replace(O1, total=20))
    assert datoms(path, Order) == [*O1_DATOMS, ("o1", "total", 20, 2, 1)]


def test_an_update_to_an_equal_record_adds_no_datom(db, path):
    db.update(O1)
    assert datoms(path, Order) == O1_DATOMS


def test_the_same_key_twice_in_one_update_keeps_the_last(db):
    db.update(replace(O1, total=1), replace(O1, total=2))
    assert [o.total for o in db[Order]] == [2]


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
def test_ending_a_record_retracts_each_field_with_its_value(db, path, end):
    end(db)
    assert len(db[Order]) == 0
    retracted = [("o1", "customer", "ana", 2, 0), ("o1", "id", "o1", 2, 0), ("o1", "total", 10, 2, 0)]
    assert datoms(path, Order) == sorted([*O1_DATOMS, *retracted])


@pytest.mark.parametrize(
    "write",
    [lambda db: db.remove(replace(O1, total=99)), lambda db: db[Order].delete("o1", "nope")],
)
def test_ending_a_record_that_is_not_stored_raises_and_changes_nothing(db, path, write):
    with pytest.raises(Missing):
        write(db)
    assert datoms(path, Order) == O1_DATOMS


def test_a_second_write_to_a_field_in_one_transaction_replaces_the_first(db, path):
    with db.transaction():
        db.update(replace(O1, total=20))
        db.update(replace(O1, total=30))
    assert datoms(path, Order) == [*O1_DATOMS, ("o1", "total", 30, 2, 1)]


def test_updating_a_record_that_others_refer_to_leaves_them_alone(db, path):
    db.update(replace(ANA, name="Ana B"))
    assert [o.id for o in db[Order].filter(Order.customer.name == "Ana B")] == ["o1"]
    assert datoms(path, Order) == O1_DATOMS
