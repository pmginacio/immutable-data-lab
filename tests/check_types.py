"""Checked by pyright, not run by pytest.

Each `# type: ignore` marks an expected error; pyright reports any that is no longer needed.
"""

from dataclasses import replace
from typing import Any, assert_type

from inlay import Db, RecordSet, RecordView, Ref, all_of, any_of, avg, contains, entity, key, negate, none_of, one_of
from inlay.nodes import And


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


ana = Customer(name="Ana", city="Lisbon")
o = Order(customer=ana.id, total=120)
Customer(name="Bob", city="Porto", id="c1")

assert_type(o.id, str)
assert_type(o.customer, str)
assert_type(o.total, int)
assert_type(Order.customer, type[Customer])
assert_type(Order.customer.city, str)
assert_type(replace(o, total=150), Order)


def _tables(db: Db) -> None:
    assert_type(db[Customer], RecordSet[Customer])
    assert_type(db[Customer].get("c1"), Customer | None)
    view = db[Order].filter(Order.total > 100).sorted(key=Order.total)[:10]
    assert_type(view, RecordView[Order])
    assert_type(view.get("o1"), Order | None)
    assert_type(len(view), int)
    assert_type(view.sum(Order.total), int)
    assert_type(view.min(Order.total), int)
    assert_type(view.max(Order.total, default=None), int | None)
    assert_type(o in view, bool)
    assert_type(view.aggregate_by(Order.total, len), dict[int, int])
    assert_type(view.aggregate_by(Order.total, sum, Order.total), dict[int, int])
    assert_type(view.aggregate_by(Order.total, avg, Order.total), dict[int, float])
    assert_type(view.aggregate_by((Order.id, Order.total), len), dict[tuple[str, int], int])
    # pyright cannot pick one of the overloads of the built-in `max`.
    assert_type(view.aggregate_by(Order.total, max, Order.total), dict[int, Any])
    for order in view:
        assert_type(order, Order)


def _conditions() -> None:
    assert_type(Order.total > 100, bool)
    all_of(Order.total > 100, Order.customer.city == "Lisbon")
    any_of(contains(Customer.name, "x"), Customer.name.startswith("A"))
    negate(one_of(Order.total, (1, 2)))
    none_of(Order.customer == ana.id)
    assert_type((Order.total > 1) | (Order.total == 0), bool)
    assert_type(contains(Customer.name, "x") & (Order.total > 1), And)


def _expected_errors() -> None:
    contains(Order.total, "x")  # type: ignore
    Customer("Ana", "Lisbon")  # type: ignore
    Customer(name="Ana")  # type: ignore
    Order(customer=5, total=120)  # type: ignore
    o.total = 3  # type: ignore
    Order.totl  # type: ignore
    Order.customer.nmae  # type: ignore
