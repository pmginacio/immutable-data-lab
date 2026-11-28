"""Aggregates in a projection: a value of each record over the records that point at it through a Ref."""

from datetime import UTC, datetime

import pytest

import inlay
from inlay import Aggregate, Field, Ref, default, entity, key, projection, query


@entity(indexes=[("referred_by",)])
class Customer:
    id: Field[str] = key()
    name: Field[str]
    referred_by: Ref[Customer, str | None] = default(None)


@entity
class Address:
    id: Field[str] = key()
    city: Field[str]


@entity(indexes=[("customer",), ("billing",), ("shipping",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    billing: Ref[Address]
    shipping: Ref[Address]
    amount: Field[int]
    placed: Field[datetime]
    paid: Field[datetime | None] = default(None)


@entity(indexes=[("order",)])
class OrderLine:
    id: Field[str] = key()
    order: Ref[Order]
    qty: Field[int]
    unit_price: Field[int]


def _at(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


RECORDS = [
    Customer(id="ana", name="Ana"),
    Customer(id="bob", name="Bob", referred_by="ana"),
    Customer(id="cid", name="Cid", referred_by="ana"),
    Address(id="home", city="Lisbon"),
    Address(id="work", city="Porto"),
    Order(id="o1", customer="ana", billing="home", shipping="home", amount=100, placed=_at(1), paid=_at(2)),
    Order(id="o2", customer="ana", billing="home", shipping="work", amount=50, placed=_at(5)),
    Order(id="o3", customer="bob", billing="work", shipping="work", amount=30, placed=_at(3), paid=_at(3)),
    OrderLine(id="l1", order="o1", qty=2, unit_price=30),
    OrderLine(id="l2", order="o1", qty=1, unit_price=40),
    OrderLine(id="l3", order="o3", qty=3, unit_price=10),
]


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db") as db:
        db.register(Customer, Address, Order, OrderLine)
        db.add(*RECORDS)
        yield db


def _by_id(rows):
    return {r.id: r for r in rows}


@projection(Customer)
class CustomerRow:
    id: Field[str]
    total: Field[int] = Order.amount.sum()
    orders: Field[int] = Order.id.count()
    last_order: Field[datetime | None] = Order.placed.max()
    average: Field[float | None] = Order.amount.avg()
    per_order: Field[float | None] = Order.amount.sum() / Order.id.count()
    paid: Field[int] = Order.amount.sum(filter=Order.paid != None)
    items: Field[int] = OrderLine.qty.sum(OrderLine.order).sum()
    referrals: Field[int] = Customer.id.count(Customer.referred_by)


def test_a_projection_aggregates_the_records_that_point_at_each_record(db):
    rows = _by_id(db[Customer].project(CustomerRow))
    ana, bob, cid = rows["ana"], rows["bob"], rows["cid"]
    assert (ana.total, ana.orders, ana.last_order, ana.average, ana.per_order) == (150, 2, _at(5), 75.0, 75.0)
    assert (bob.total, bob.orders, bob.last_order) == (30, 1, _at(3))


def test_over_no_records_a_count_and_a_sum_are_0_and_the_others_none(db):
    cid = _by_id(db[Customer].project(CustomerRow))["cid"]
    assert (cid.total, cid.orders, cid.paid, cid.items) == (0, 0, 0, 0)
    assert (cid.last_order, cid.average, cid.per_order) == (None, None, None)


def test_filter_keeps_only_the_records_it_matches(db):
    rows = _by_id(db[Customer].project(CustomerRow))
    assert (rows["ana"].paid, rows["bob"].paid) == (100, 30)


def test_an_aggregate_of_an_aggregate_goes_two_references_back(db):
    rows = _by_id(db[Customer].project(CustomerRow))
    assert (rows["ana"].items, rows["bob"].items) == (3, 3)


def test_a_reference_to_its_own_class_is_named(db):
    rows = _by_id(db[Customer].project(CustomerRow))
    assert (rows["ana"].referrals, rows["bob"].referrals) == (2, 0)


def test_two_references_to_one_class_are_named(db):
    @projection(Address)
    class AddressRow:
        id: Field[str]
        billed: Field[int] = Order.id.count(Order.billing)
        shipped: Field[int] = Order.id.count(Order.shipping)

    rows = _by_id(db[Address].project(AddressRow))
    assert (rows["home"].billed, rows["home"].shipped, rows["work"].billed, rows["work"].shipped) == (2, 1, 1, 2)


def test_a_value_computed_on_each_record_is_aggregated(db):
    @projection(Order)
    class OrderRow:
        id: Field[str]
        value: Field[int] = (OrderLine.qty * OrderLine.unit_price).sum()
        distinct_prices: Field[int | None] = OrderLine.unit_price.agg(
            Aggregate("distinct", lambda values: len(set(values)), "COUNT(DISTINCT {})", arg=int, result=int)
        )

    rows = _by_id(db[Order].project(OrderRow))
    assert (rows["o1"].value, rows["o1"].distinct_prices, rows["o2"].value) == (100, 2, 0)


def test_filters_and_sorts_on_an_aggregate_come_after_the_projection(db):
    rows = db[Customer].project(CustomerRow).filter(CustomerRow.total > 0).sorted(key=CustomerRow.total)
    assert [r.id for r in rows] == ["bob", "ana"]


@pytest.mark.parametrize(
    "value, message",
    [
        (Address.city.count(), "count of Address.city needs the Ref to aggregate along named, since Address has 0 Refs"),
        (Customer.id.count(), "needs the Ref to aggregate along named, since Customer refers to itself"),
        (Order.id.count(Order.billing), "Order.billing points at Address, not Customer"),
    ],
)
def test_an_aggregate_with_no_clear_ref_is_refused_when_the_class_is_defined(value, message):
    with pytest.raises(TypeError, match=message):
        projection(Customer)(type("Wrong", (), {"__annotations__": {"id": Field[str], "x": Field[int]}, "x": value}))


def test_an_aggregate_of_an_aggregate_needs_the_inner_ref_named():
    with pytest.raises(TypeError, match="an aggregate of an aggregate needs the inner one's Ref named"):
        OrderLine.qty.sum().sum()


def test_two_refs_to_the_projected_class_are_refused_unnamed():
    with pytest.raises(TypeError, match="since Order has 2 Refs to Address"):
        projection(Address)(type("Wrong", (), {"__annotations__": {"id": Field[str], "x": Field[int]}, "x": Order.id.count()}))


def test_an_aggregate_is_typed_as_it_can_be_none():
    with pytest.raises(TypeError, match=r"declared Field\[datetime\], but its value is a datetime \| None"):
        projection(Customer)(
            type("Wrong", (), {"__annotations__": {"id": Field[str], "x": Field[datetime]}, "x": Order.placed.max()})
        )


@pytest.mark.parametrize(
    "build",
    [
        lambda: query(Customer).filter(Order.amount.sum(Order.customer) > 10),
        lambda: query(Customer).sorted(key=Order.amount.sum(Order.customer)),
        lambda: query(Order).aggregate_by(Order.customer, sum, OrderLine.qty.sum(OrderLine.order)),
    ],
)
def test_an_aggregate_outside_a_projection_is_refused(build):
    with pytest.raises(TypeError, match="can only be the value of a projection field"):
        build()
