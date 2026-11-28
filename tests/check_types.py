"""Checked by pyright, not run by pytest.

Each `# type: ignore` marks an expected error; pyright reports any that is no longer needed.
"""

from dataclasses import replace
from datetime import date
from typing import Any, assert_type

from inlay import (
    Field,
    Db,
    Grouped,
    Query,
    RecordSet,
    Ref,
    all_of,
    any_of,
    avg,
    coalesce,
    default,
    entity,
    key,
    query,
    projection,
    to_month,
)
from inlay.conditions import Expr
from inlay.nodes import And, Condition, Not, Or


@entity(indexes=[("city",)])
class Customer:
    id: Field[str] = key()
    name: Field[str]
    city: Field[str]


@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]
    placed: Field[date]
    discount: Field[int | None] = default(None)


ana = Customer(name="Ana", city="Lisbon")
o = Order(customer=ana.id, total=120, placed=date(2026, 1, 1))
Customer(name="Bob", city="Porto", id="c1")

assert_type(o.id, str)
assert_type(o.customer, str)
assert_type(o.total, int)
assert_type(o.discount, int | None)
assert_type(Order.total, Expr[int])
assert_type(Order.discount, Expr[int | None])
assert_type(Order.customer, type[Customer])
assert_type(Order.customer.city, Expr[str])
assert_type(to_month(Order.placed), Expr[date])
assert_type(to_month(date(2026, 1, 5)), date)
assert_type(replace(o, total=150), Order)
assert_type(coalesce(Order.customer.city, "unknown"), Expr[str])
assert_type(coalesce(Order.discount, 0), Expr[int])
assert_type(coalesce(None, 3), int | None)
assert_type(Order.total.sum(), Expr[int])
assert_type(Order.total.max(), Expr[int | None])
assert_type(Order.id.count(Order.customer), Expr[int])


def _tables(db: Db) -> None:
    assert_type(db[Customer], RecordSet[Customer])
    assert_type(db[Customer].get("c1"), Customer | None)
    view = db[Order].filter(Order.total > 100).sorted(key=Order.total)[:10]
    assert_type(view, Query[Order])
    assert_type(view.get("o1"), Order | None)
    assert_type(len(view), int)
    assert_type(view.sum(Order.total), int)
    assert_type(view.min(Order.total), int | None)
    assert_type(view.max(Order.total, default=0), int)
    assert_type(o in view, bool)
    assert_type(view.aggregate_by(Order.total, len), Grouped[int, int])
    assert_type(view.aggregate_by(Order.total, sum, Order.total), Grouped[int, int])
    assert_type(view.aggregate_by(Order.total, avg, Order.total), Grouped[int, float])
    assert_type(view.aggregate_by((Order.id, Order.total), len), Grouped[tuple[str, int], int])
    # pyright cannot pick one of the overloads of the built-in `max`.
    assert_type(view.aggregate_by(Order.total, max, Order.total), Grouped[int, Any])
    for order in view:
        assert_type(order, Order)
    rows = db[Order].project(OrderRow).filter(OrderRow.city == "Lisbon")
    assert_type(rows, Query[OrderRow])
    for row in rows:
        assert_type(row.city, str)
    big = query(Order).filter(Order.total > 100)
    assert_type(big, Query[Order])
    assert_type(db[big], Query[Order])
    assert_type(db[big.aggregate_by(Order.total, len)], Grouped[int, int])
    by_customer = big.aggregate_by(Order.customer, len)
    assert_type(by_customer, Grouped[str, int])
    assert_type(by_customer[Customer.id], Expr[int | None])
    assert_type(by_customer[Order.customer], Expr[int | None])


@projection(Order)
class OrderRow:
    id: Field[str]
    city: Field[str] = Order.customer.city


def _conditions() -> None:
    assert_type(Order.total > 100, Condition)
    all_of(Order.total > 100, Order.customer.city == "Lisbon")
    any_of(Customer.name.contains("x"), Customer.name.startswith("A"))
    assert_type(~Order.total.one_of((1, 2)), Not)
    assert_type((Order.total > 1) | (Order.total == 0), Or)
    assert_type(Customer.name.contains("x") & (Order.total > 1), And)


def _expected_errors() -> None:
    Order.total.contains("x")  # type: ignore
    Customer("Ana", "Lisbon")  # type: ignore
    Customer(name="Ana")  # type: ignore
    Order(customer=5, total=120, placed=date(2026, 1, 1))  # type: ignore
    Order(customer=ana.id, total="120", placed=date(2026, 1, 1))  # type: ignore
    o.total = 3  # type: ignore
    Order.totl  # type: ignore
    Order.customer.nmae  # type: ignore
    OrderRow(id="o1").city = "Faro"  # type: ignore
