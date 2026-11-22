"""Values read through a `Ref`, such as `Order.customer.name`, as sort keys, group keys and aggregated values."""

import pytest
from helpers import opened

import inlay
import inlay.datoms
import inlay.history
from inlay import Ref, entity, key


@entity(indexes=[("referred_by",)])
class Customer:
    id: str = key()
    name: str
    city: str
    referred_by: Ref[Customer, str | None]


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity(indexes=[("order",)])
class Line:
    id: str = key()
    order: Ref[Order]
    qty: int


RECORDS = [
    Customer(id="ana", name="Ana", city="Lisbon", referred_by=None),
    Customer(id="bob", name="Bob", city="Porto", referred_by="ana"),
    Customer(id="cid", name="Cid", city="Lisbon", referred_by="bob"),
    Order(id="o1", customer="cid", total=10),
    Order(id="o2", customer="ana", total=20),
    Order(id="o3", customer="bob", total=30),
    Order(id="o4", customer="ana", total=40),
    Line(id="l1", order="o1", qty=1),
    Line(id="l2", order="o2", qty=2),
    Line(id="l3", order="o3", qty=4),
]


@pytest.fixture
def db(tmp_path):
    with opened(inlay.open, [Customer, Order, Line], tmp_path / "shop.db", RECORDS) as db:
        yield db


def ids(view):
    return [r.id for r in view]


def test_sorting_by_a_value_of_the_referenced_record(db):
    assert ids(db[Order].sorted(key=Order.customer.name)) == ["o2", "o4", "o3", "o1"]
    assert ids(db[Order].sorted(key=(Order.customer.city, Order.total), reverse=True)) == ["o3", "o4", "o2", "o1"]
    # A filter after a slice reads the slice as a subquery, which sorts again.
    assert ids(db[Order].sorted(key=Order.customer.name)[:3].filter(Order.total > 15)) == ["o2", "o4", "o3"]


def test_grouping_and_aggregating_through_several_refs(db):
    assert db[Line].aggregate_by(Line.order.customer.city, sum, Line.qty) == {"Lisbon": 3, "Porto": 4}
    assert db[Order].max(Order.customer.name) == "Cid"


def test_an_empty_ref_gives_none(db):
    assert db[Customer].aggregate_by(Customer.referred_by.referred_by.name, len) == {None: 2, "Ana": 1}
    assert ids(db[Customer].sorted(key=Customer.referred_by.name)) == ["ana", "bob", "cid"]


def test_a_table_reached_by_a_sort_key_cannot_change_while_looping(db):
    with pytest.raises(RuntimeError, match="Customer changed during iteration"):
        for _ in db[Order].sorted(key=Order.customer.name):
            db.update(RECORDS[0])


@pytest.mark.parametrize("open_db", [inlay.history.open, inlay.datoms.open], ids=["history", "datoms"])
def test_the_paused_history_experiments_refuse_them(open_db, tmp_path):
    # The experiments refuse optional fields too, so they get classes without one.
    @entity
    class Client:
        id: str = key()
        name: str

    @entity(indexes=[("customer",)])
    class Purchase:
        id: str = key()
        customer: Ref[Client]
        total: int

    with opened(open_db, [Client, Purchase], tmp_path / "experiment.db") as db:
        with pytest.raises(TypeError, match="Purchase.customer.name"):
            list(db[Purchase].sorted(key=Purchase.customer.name))
