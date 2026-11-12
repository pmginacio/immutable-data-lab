import sqlite3
from datetime import datetime

import pytest

from inlay import Ref, all_of, any_of, contains, entity, key, negate, none_of, one_of
from inlay.meta import meta
from inlay.sql import condition_sql, create_table_sql, insert_sql, quote


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
    discount: int


@entity(indexes=[("order",)])
class OrderLine:
    id: str = key()
    order: Ref[Order]


@entity
class Event:
    id: str = key()
    at: datetime


RECORDS = [
    Customer(id="ana", name="Ana", city="Lisbon"),
    Customer(id="anabel", name="Anabel", city="Lisbon"),
    Customer(id="bob", name="Bob", city="Porto"),
    Customer(id="rita", name="Rita", city="Faro"),
    Order(id="o1", customer="ana", total=150, discount=10),
    Order(id="o2", customer="anabel", total=50, discount=60),
    Order(id="o3", customer="bob", total=100, discount=0),
    OrderLine(id="l1", order="o1"),
    OrderLine(id="l2", order="o3"),
]
ALL_CUSTOMERS = {"ana", "anabel", "bob", "rita"}


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    for cls in (Customer, Order, OrderLine):
        conn.execute(create_table_sql(meta(cls)))
    for record in RECORDS:
        m = meta(type(record))
        conn.execute(insert_sql(m), [getattr(record, f.name) for f in m.fields])
    yield conn
    conn.close()


@pytest.mark.parametrize(
    ("cls", "cond", "expected"),
    [
        (Customer, Customer.city == "Lisbon", {"ana", "anabel"}),
        (Customer, Customer.city != "Lisbon", {"bob", "rita"}),
        (Customer, Customer.name.startswith("An"), {"ana", "anabel"}),
        (Customer, Customer.name.startswith("an"), set()),
        (Customer, Customer.name.startswith(""), ALL_CUSTOMERS),
        (Customer, Customer.name.endswith("a"), {"ana", "rita"}),
        (Customer, Customer.name.endswith("Ana"), {"ana"}),
        (Customer, Customer.name.endswith(""), ALL_CUSTOMERS),
        (Customer, Customer.name.endswith("Anabel and more"), set()),
        (Customer, contains(Customer.name, "nab"), {"anabel"}),
        (Customer, contains(Customer.name, ""), ALL_CUSTOMERS),
        (Customer, one_of(Customer.city, ("Lisbon", "Faro")), {"ana", "anabel", "rita"}),
        (Customer, one_of(Customer.city, ()), set()),
        (Customer, (Customer.city == "Lisbon") & Customer.name.startswith("Anab"), {"anabel"}),
        (Customer, (Customer.city == "Porto") | (Customer.city == "Faro"), {"bob", "rita"}),
        (Customer, negate(Customer.city == "Lisbon"), {"bob", "rita"}),
        (Customer, none_of(Customer.city == "Lisbon", Customer.city == "Porto"), {"rita"}),
        (Customer, all_of(), ALL_CUSTOMERS),
        (Customer, any_of(), set()),
        (Customer, none_of(), ALL_CUSTOMERS),
        (Order, Order.total > Order.discount, {"o1", "o3"}),
        (Order, 100 < Order.total, {"o1"}),
        (Order, Order.customer == "bob", {"o3"}),
        (Order, Order.customer.city == "Lisbon", {"o1", "o2"}),
        (Order, negate(Order.customer.city == "Lisbon"), {"o3"}),
        (Order, one_of(Order.customer.city, ("Porto", "Faro")), {"o3"}),
        (Order, Order.customer.name.startswith("An") & (Order.total >= 100), {"o1"}),
        (OrderLine, OrderLine.order.customer.city == "Lisbon", {"l1"}),
        (OrderLine, OrderLine.order.customer.city == "Porto", {"l2"}),
    ],
)
def test_condition_selects_the_matching_records(conn, cls, cond, expected):
    sql, params = condition_sql(cond)
    rows = conn.execute(f"SELECT id FROM {quote(meta(cls).table)} WHERE {sql}", params)
    assert {id_ for (id_,) in rows} == expected


def test_values_are_parameters():
    assert condition_sql(Customer.name == "a'b") == ('"name" = ?', ["a'b"])


def test_datetimes_are_stored_as_text():
    _, params = condition_sql(Event.at > datetime(2026, 1, 1))
    assert params == ["2026-01-01T00:00:00"]


def test_relations_become_nested_subqueries():
    assert condition_sql(Order.customer.city == "Lisbon") == (
        '"customer" IN (SELECT "id" FROM "customer" WHERE "city" = ?)',
        ["Lisbon"],
    )
    assert condition_sql(OrderLine.order.customer.city == "Lisbon") == (
        '"order" IN (SELECT "id" FROM "order" WHERE "customer" IN (SELECT "id" FROM "customer" WHERE "city" = ?))',
        ["Lisbon"],
    )
