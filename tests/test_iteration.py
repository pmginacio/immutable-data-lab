from dataclasses import replace

import pytest

import inlay
from inlay import Ref, entity, key


@entity(indexes=[("city",)])
class Customer:
    id: str = key()
    name: str
    city: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity
class Event:
    id: str = key()
    name: str


ANA = Customer(id="ana", name="Ana", city="Lisbon")
BOB = Customer(id="bob", name="Bob", city="Porto")


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db", [Customer, Order, Event]) as db:
        db.add(ANA, BOB, Order(customer="ana", total=10))
        yield db


@pytest.mark.parametrize(
    "write",
    [
        lambda db: db.add(Customer(name="Dee", city="Faro")),
        lambda db: db.update(ANA),
        lambda db: db.remove(ANA),
        lambda db: db.discard(ANA),
        lambda db: db[Customer].delete("ana"),
        lambda db: db[Customer].clear(),
    ],
)
def test_writing_to_a_table_being_iterated_raises(db, write):
    with pytest.raises(RuntimeError, match="Customer changed during iteration"):
        for _ in db[Customer]:
            write(db)
    assert set(db[Customer]) == {ANA, BOB}


def test_writing_to_other_tables_is_fine(db):
    for customer in db[Customer]:
        db.add(Order(customer=customer.id, total=1), Event(name="seen"))
    assert len(db[Order]) == 3


def test_tables_reached_by_relations_count_too(db):
    lisbon_orders = db[Order].filter(Order.customer.city == "Lisbon")
    with pytest.raises(RuntimeError, match="Customer changed during iteration"):
        for _ in lisbon_orders:
            db.update(ANA)
    with pytest.raises(RuntimeError, match="Order changed during iteration"):
        for _ in lisbon_orders:
            db.add(Order(customer="ana", total=2))
    for _ in lisbon_orders:
        db.add(Event(name="fine"))


def test_tables_reached_in_the_source_of_a_slice_count_too(db):
    first = db[Order].filter(Order.customer.city == "Lisbon")[:1].sorted(key=Order.total)
    with pytest.raises(RuntimeError, match="Customer changed during iteration"):
        for _ in first:
            db.update(ANA)


def test_breaking_out_of_the_loop_allows_writes_again(db):
    for _ in db[Customer]:
        break
    db.update(replace(ANA, city="Faro"))


def test_an_error_in_the_loop_allows_writes_again(db):
    with pytest.raises(ZeroDivisionError):
        for _ in db[Customer]:
            1 / 0
    db.update(replace(ANA, city="Faro"))


def test_an_iterator_you_hold_blocks_writes_until_it_is_closed(db):
    it = iter(db[Customer])
    next(it)
    with pytest.raises(RuntimeError, match="Customer changed during iteration"):
        db.update(ANA)
    it.close()
    db.update(replace(ANA, city="Faro"))


def test_a_snapshot_can_be_changed_while_looping(db):
    for customer in tuple(db[Customer]):
        db.update(replace(customer, city="Faro"))
    assert {c.city for c in db[Customer]} == {"Faro"}
