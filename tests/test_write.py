import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

import inlay
import inlay.history
from inlay import AlreadyExists, BrokenRef, Missing, Ref, entity, key


@entity
class Customer:
    id: str = key()
    name: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity(indexes=[("parent",)])
class Category:
    id: str = key()
    parent: Ref[Category]
    name: str


@entity
class Everything:
    id: str = key()
    i: int
    f: float
    s: str
    b: bytes
    flag: bool
    at: datetime
    day: date


# Every write test also runs against the history experiment, which must behave the same for the current data.
@pytest.fixture(params=[inlay.open, inlay.history.open], ids=["inlay", "history"])
def open_db(request):
    return request.param


@pytest.fixture
def db(open_db, tmp_path):
    with open_db([Customer, Order, Category, Everything], path=tmp_path / "shop.db") as db:
        yield db


def test_add_and_get(db):
    ana = Customer(name="Ana")
    db.add(ana)
    assert db[Customer].get(ana.id) == ana
    assert db[Customer].get("nope") is None


def test_values_round_trip(db):
    e = Everything(i=1, f=1.5, s="x", b=b"\x00", flag=True, at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC), day=date(2026, 1, 2))
    db.add(e)
    assert db[Everything].get(e.id) == e


def test_aware_datetimes_compare_by_moment_whatever_their_offset(db):
    def at(moment: datetime) -> Everything:
        return Everything(i=1, f=1.0, s="", b=b"", flag=False, at=moment, day=date(2026, 1, 1))

    early = at(datetime(2026, 1, 1, 10, tzinfo=timezone(timedelta(hours=2))))  # 08:00 UTC
    late = at(datetime(2026, 1, 1, 9, tzinfo=UTC))
    db.add(early, late)
    view = db[Everything]
    assert (view.min(Everything.at), view.max(Everything.at)) == (early.at, late.at)
    assert [e.id for e in view.sorted(key=Everything.at)] == [early.id, late.id]
    assert set(view.filter(Everything.at > datetime(2026, 1, 1, 8, 30, tzinfo=UTC))) == {late}
    assert early in view
    db.remove(early)


def test_add_existing_key_fails_and_writes_nothing(db):
    ana = Customer(name="Ana")
    db.add(ana)
    bob = Customer(name="Bob")
    with pytest.raises(AlreadyExists):
        db.add(bob, replace(ana, name="Other"))
    assert db[Customer].get(bob.id) is None
    assert db[Customer].get(ana.id) == ana


def test_order_inside_a_call_does_not_matter(db):
    ana = Customer(name="Ana")
    order = Order(customer=ana.id, total=1)
    db.add(order, ana)
    assert db[Order].get(order.id) == order

    root = Category(id="root", parent="root", name="All")
    child = Category(parent="root", name="Books")
    db.add(child, root)
    assert db[Category].get(child.id) == child


def test_ref_to_missing_object_fails_and_writes_nothing(db):
    order = Order(customer="nobody", total=1)
    with pytest.raises(BrokenRef):
        db.add(order)
    assert db[Order].get(order.id) is None


def test_update_replaces_or_inserts(db):
    ana = Customer(name="Ana")
    db.update(ana)
    db.update(replace(ana, name="Ana Maria"))
    assert db[Customer].get(ana.id) == replace(ana, name="Ana Maria")


def test_remove_only_equal_objects(db):
    ana = Customer(name="Ana")
    db.add(ana)
    with pytest.raises(Missing):
        db.remove(replace(ana, name="stale"))
    assert db[Customer].get(ana.id) == ana
    db.remove(ana)
    assert db[Customer].get(ana.id) is None


def test_discard_ignores_objects_not_stored(db):
    ana = Customer(name="Ana")
    db.add(ana)
    db.discard(ana, Customer(name="Bob"))
    assert db[Customer].get(ana.id) is None


def test_delete_by_key(db):
    ana, bob = Customer(name="Ana"), Customer(name="Bob")
    db.add(ana, bob)
    with pytest.raises(Missing):
        db[Customer].delete(ana.id, "nope")
    assert db[Customer].get(ana.id) == ana
    db[Customer].delete(ana.id)
    assert db[Customer].get(ana.id) is None


@pytest.mark.parametrize(
    "end", [lambda customers, ana: customers.delete(ana.id), lambda customers, ana: customers.clear()], ids=["delete", "clear"]
)
def test_delete_referenced_object_fails(db, end):
    ana = Customer(name="Ana")
    db.add(ana, Order(customer=ana.id, total=1))
    with pytest.raises(BrokenRef):
        end(db[Customer], ana)
    assert db[Customer].get(ana.id) == ana


def test_clear(db):
    ana = Customer(name="Ana")
    db.add(ana)
    db[Customer].clear()
    assert db[Customer].get(ana.id) is None


def test_clear_with_conditions_deletes_only_matching_records(db):
    ana, bob = Customer(name="Ana"), Customer(name="Bob")
    small, big = Order(customer=ana.id, total=5), Order(customer=bob.id, total=500)
    db.add(ana, bob, small, big)
    db[Order].clear(Order.total > 100, Order.customer.name == "Bob")
    assert set(db[Order]) == {small}
    db[Order].clear(Order.total > 100)
    assert set(db[Order]) == {small}


def test_clear_with_a_broken_condition_changes_nothing(db):
    ana = Customer(name="Ana")
    db.add(ana)
    with pytest.raises(ValueError, match="cannot be used on Customer"):
        db[Customer].clear(Order.total > 1)
    assert db[Customer].get(ana.id) == ana


def test_transaction_rolls_back_on_error(db):
    ana, bob = Customer(name="Ana"), Customer(name="Bob")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.add(ana)
            with db.transaction():
                db.add(bob)
            raise RuntimeError
    assert db[Customer].get(ana.id) is None
    assert db[Customer].get(bob.id) is None


def test_broken_ref_in_transaction_is_raised_at_the_end(db):
    ana = Customer(name="Ana")
    with pytest.raises(BrokenRef):
        with db.transaction():
            db.add(Order(customer=ana.id, total=1))
            reached_end_of_block = True
    assert reached_end_of_block


def test_store_errors_inside_a_transaction_can_be_caught(db):
    ana = Customer(name="Ana")
    db.add(ana)
    bob = Customer(name="Bob")
    with db.transaction():
        with pytest.raises(AlreadyExists):
            db.add(ana)
        db.add(bob)
    assert db[Customer].get(bob.id) == bob


def test_failed_call_inside_a_transaction_writes_nothing(db):
    ana, bob = Customer(name="Ana"), Customer(name="Bob")
    db.add(ana)
    with db.transaction():
        with pytest.raises(AlreadyExists):
            db.add(bob, ana)
        assert db[Customer].get(bob.id) is None
    assert db[Customer].get(bob.id) is None


def test_unknown_class(db):
    @entity
    class Other:
        id: str = key()

    with pytest.raises(KeyError, match="Other was not passed to"):
        db[Other]
    with pytest.raises(KeyError):
        db.add(Other())


def test_closed_db(open_db, tmp_path):
    with open_db([Customer], path=tmp_path / "shop.db") as db:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        db[Customer].get("x")
