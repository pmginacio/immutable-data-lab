from dataclasses import replace
from datetime import UTC, datetime

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
    at: datetime


ANA = Customer(id="ana", name="Ana", city="Lisbon")
BOB = Customer(id="bob", name="Bob", city="Porto")
CID = Customer(id="cid", name="Cid", city="Lisbon")
ORDERS = [
    Order(id="o1", customer="ana", total=150),
    Order(id="o2", customer="ana", total=50),
    Order(id="o3", customer="bob", total=200),
    Order(id="o4", customer="cid", total=120),
]


EVENTS = [
    Event(id="e1", at=datetime(2026, 3, 1, tzinfo=UTC)),
    Event(id="e2", at=datetime(2026, 1, 1, tzinfo=UTC)),
]


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db", [Customer, Order, Event]) as db:
        db.add(ANA, BOB, CID, *ORDERS, *EVENTS)
        yield db


def ids(view):
    return [r.id for r in view]


def test_iterating_gives_whole_records(db):
    assert set(db[Customer]) == {ANA, BOB, CID}


def test_every_read_sees_current_data(db):
    customers = db[Customer]
    snapshot = tuple(customers)
    db.add(Customer(name="Dee", city="Faro"))
    assert len(customers) == 4
    assert len(snapshot) == 3


def test_filter_takes_conditions_and_relations(db):
    big_in_lisbon = db[Order].filter(Order.total > 100, Order.customer.city == "Lisbon")
    assert set(ids(big_in_lisbon)) == {"o1", "o4"}


def test_filter_checks_its_conditions_right_away(db):
    with pytest.raises(ValueError, match="Customer.name cannot be used on Order"):
        db[Order].filter(Customer.name == "Ana")
    with pytest.raises(TypeError, match="expected a condition"):
        db[Order].filter(True)


def test_filter_returns_a_new_view(db):
    orders = db[Order]
    big = orders.filter(Order.total > 100)
    assert (len(orders), len(big)) == (4, 3)


def test_sorted(db):
    assert ids(db[Order].sorted(key=Order.total)) == ["o2", "o4", "o1", "o3"]
    assert ids(db[Order].sorted(key=Order.total, reverse=True)) == ["o3", "o1", "o4", "o2"]


def test_sorted_by_several_fields(db):
    assert ids(db[Customer].sorted(key=(Customer.city, Customer.name), reverse=True)) == ["bob", "cid", "ana"]


def test_sorted_needs_fields(db):
    by_customer = sorted(db[Order], key=lambda o: (o.customer, o.id))
    assert list(db[Order].sorted(key=Order.customer)) == by_customer
    with pytest.raises(TypeError, match="expected a field"):
        db[Order].sorted(key="total")
    with pytest.raises(ValueError, match="Customer.city is not a field of Order"):
        db[Order].sorted(key=Order.customer.city)
    with pytest.raises(ValueError, match="Customer.name is not a field of Order"):
        db[Order].sorted(key=Customer.name)


def test_slices(db):
    by_total = db[Order].sorted(key=Order.total)
    assert ids(by_total[:2]) == ["o2", "o4"]
    assert ids(by_total[1:3]) == ["o4", "o1"]
    assert ids(by_total[2:]) == ["o1", "o3"]
    assert len(by_total[1:3]) == 2


def test_conditions_after_a_slice_apply_to_the_slice(db):
    first_two = db[Order].sorted(key=Order.total)[:2]
    assert ids(first_two.filter(Order.customer == "ana")) == ["o2"]


@pytest.mark.parametrize("bad", [slice(None, None, 2), slice(-1, None), slice(None, -1)])
def test_slices_take_start_and_stop_only(db, bad):
    with pytest.raises(ValueError, match="no negative numbers"):
        db[Order][bad]


def test_a_view_cannot_be_indexed(db):
    with pytest.raises(TypeError, match="can only be sliced"):
        db[Order][0]


def test_get_looks_only_inside_the_view(db):
    big = db[Order].filter(Order.total > 100)
    assert big.get("o1") == ORDERS[0]
    assert big.get("o2") is None
    assert big.get("nope") is None


def test_in_needs_an_equal_record(db):
    assert ANA in db[Customer]
    assert replace(ANA, city="Faro") not in db[Customer]
    assert ORDERS[0] not in db[Customer]
    assert ANA not in db[Customer].filter(Customer.city == "Porto")


def test_in_looks_inside_a_slice(db):
    from_bob_on = db[Customer].sorted(key=Customer.name)[1:]
    assert BOB in from_bob_on
    assert ANA not in from_bob_on


def test_sum(db):
    assert db[Order].sum(Order.total) == 520
    assert db[Order].filter(Order.customer == "ana").sum(Order.total) == 200
    assert db[Order].sorted(key=Order.total)[:2].sum(Order.total) == 170
    assert db[Order].filter(Order.total > 999).sum(Order.total) == 0


def test_min_and_max(db):
    assert db[Order].min(Order.total) == 50
    assert db[Order].max(Order.total) == 200
    assert db[Customer].filter(Customer.city == "Lisbon").max(Customer.name) == "Cid"
    assert db[Event].min(Event.at) == datetime(2026, 1, 1, tzinfo=UTC)


def test_min_and_max_of_nothing_need_a_default(db):
    nothing = db[Order].filter(Order.total > 999)
    with pytest.raises(ValueError, match="min.. of an empty view"):
        nothing.min(Order.total)
    with pytest.raises(ValueError, match="max.. of an empty view"):
        nothing.max(Order.total)
    assert nothing.min(Order.total, default=0) == 0
    assert nothing.max(Order.total, None) is None


def test_aggregates_need_a_field_of_the_class_itself(db):
    with pytest.raises(ValueError, match="Customer.city is not a field of Order"):
        db[Order].min(Order.customer.city)
    assert db[Order].min(Order.customer) == min(o.customer for o in db[Order])


def test_only_the_record_set_can_delete(db):
    view = db[Order].filter(Order.total > 100)
    assert not hasattr(view, "delete")
    assert not hasattr(view, "clear")
