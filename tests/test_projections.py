"""Projections: read-only shapes of the records of one class, with values from the records its `Ref`s reach."""

from datetime import UTC, date, datetime

import pytest

import inlay
from inlay import Ref, entity, key, projection, query, to_month


@entity
class Customer:
    id: str = key()
    name: str
    city: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer, str | None]
    total: int
    placed: datetime


@projection(Order)
class OrderRow:
    id: str
    total: int
    city: str | None = Order.customer.city
    month: date = to_month(Order.placed)
    with_tax: float = Order.total * 1.25


first_orders = query(Order).aggregate_by(Order.customer, min, Order.placed)


@projection(Order)
class OrderWithFirst:
    id: str
    customers_first: datetime | None = first_orders[Order.customer]


RECORDS = [
    Customer(id="ana", name="Ana", city="Lisbon"),
    Customer(id="bob", name="Bob", city="Porto"),
    Order(id="o1", customer="ana", total=100, placed=datetime(2026, 1, 5, tzinfo=UTC)),
    Order(id="o2", customer="bob", total=40, placed=datetime(2026, 2, 7, tzinfo=UTC)),
    Order(id="o3", customer=None, total=60, placed=datetime(2026, 2, 9, tzinfo=UTC)),
]
O1 = OrderRow(id="o1", total=100, city="Lisbon", month=date(2026, 1, 1), with_tax=125.0)


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db") as db:
        db.register(Customer, Order)
        db.add(*RECORDS)
        yield db


def ids(view):
    return [r.id for r in view]


def test_a_projection_reads_its_values_and_none_through_an_empty_ref(db):
    rows = {r.id: r for r in db[Order].project(OrderRow)}
    assert rows["o1"] == O1
    assert (rows["o3"].city, rows["o3"].month) == (None, date(2026, 2, 1))


def test_conditions_on_the_root_go_before_it_and_on_its_fields_after_it(db):
    rows = db[Order].filter(Order.total > 50).project(OrderRow).filter(OrderRow.city == "Lisbon")
    assert ids(rows) == ["o1"]
    with pytest.raises(ValueError, match="Order.total cannot be used on OrderRow"):
        db[Order].project(OrderRow).filter(Order.total > 50)


def test_a_projected_view_sorts_slices_and_aggregates_by_its_fields(db):
    rows = db[Order].project(OrderRow)
    assert ids(rows.sorted(key=OrderRow.with_tax, reverse=True)[:2]) == ["o1", "o3"]
    assert (len(rows), rows.sum(OrderRow.total), rows.max(OrderRow.city)) == (3, 200, "Porto")
    assert rows.aggregate_by(OrderRow.month, len) == {date(2026, 1, 1): 1, date(2026, 2, 1): 2}
    assert rows.get("o1") == O1
    assert O1 in rows


def test_a_projected_query_built_without_a_db_runs_on_one(db):
    lisbon = query(Order).project(OrderRow).filter(OrderRow.city == "Lisbon")
    assert ids(db[lisbon]) == ["o1"]


def test_a_field_can_be_the_value_of_a_group(db):
    db.add(Order(id="o4", customer="ana", total=10, placed=datetime(2026, 3, 1, tzinfo=UTC)))
    rows = {r.id: r.customers_first for r in db[Order].project(OrderWithFirst)}
    first = datetime(2026, 1, 5, tzinfo=UTC)
    assert rows == {"o1": first, "o2": datetime(2026, 2, 7, tzinfo=UTC), "o3": datetime(2026, 2, 9, tzinfo=UTC), "o4": first}


def test_sorting_or_slicing_before_it_is_refused(db):
    with pytest.raises(ValueError, match="project comes after filters only"):
        db[Order].sorted(key=Order.total).project(OrderRow)
    with pytest.raises(ValueError, match="project comes after filters only"):
        db[Order][:2].project(OrderRow)


def test_it_projects_its_root_only(db):
    with pytest.raises(TypeError, match="OrderRow is a projection of Order, not of Customer"):
        db[Customer].project(OrderRow)
    with pytest.raises(TypeError, match="Order is not a @projection class"):
        db[Order].project(Order)


def test_it_has_no_table(db):
    with pytest.raises(TypeError, match="OrderRow is not an @entity class"):
        db[OrderRow]
    with pytest.raises(TypeError, match="OrderRow is not an @entity class"):
        db.register(OrderRow)


def test_writing_a_table_it_reads_while_looping_is_refused(db):
    with pytest.raises(RuntimeError, match="Customer changed during iteration"):
        for _ in db[Order].project(OrderRow):
            db.update(RECORDS[0])


def test_it_needs_the_key_of_the_root():
    with pytest.raises(TypeError, match="NoKey needs a field with the key of Order, id"):

        @projection(Order)
        class NoKey:
            total: int


def test_a_field_not_on_the_root_needs_a_value():
    with pytest.raises(TypeError, match="Unknown.name is not a field of Order, so it needs a value"):

        @projection(Order)
        class Unknown:
            id: str
            name: str


@pytest.mark.parametrize(
    "hint, value, message",
    [
        (int, Order.customer.name, "typed <class 'int'>, but its value is a str | None"),
        (str, Order.customer.name, "typed <class 'str'>, but its value is a str | None"),
        (int | None, Order.total, r"typed int \| None, but its value is a int"),
    ],
)
def test_each_field_is_typed_as_its_value(hint, value, message):
    with pytest.raises(TypeError, match=message):
        projection(Order)(type("Wrong", (), {"__annotations__": {"id": str, "x": hint}, "x": value}))


def test_values_come_from_the_root():
    with pytest.raises(ValueError, match="Customer.name is not a field of Order"):

        @projection(Order)
        class Other:
            id: str
            name: str = Customer.name
