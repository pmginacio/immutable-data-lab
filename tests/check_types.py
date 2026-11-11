"""Checked by pyright, not run by pytest.

Each `# type: ignore` marks an expected error; pyright reports any that is no longer needed.
"""

from dataclasses import replace
from typing import assert_type

from inlay import Db, Ref, Table, entity, key


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
    assert_type(db[Customer], Table[Customer])
    assert_type(db[Customer].get("c1"), Customer | None)


def _expected_errors() -> None:
    Customer("Ana", "Lisbon")  # type: ignore
    Customer(name="Ana")  # type: ignore
    Order(customer=5, total=120)  # type: ignore
    o.total = 3  # type: ignore
    Order.totl  # type: ignore
    Order.customer.nmae  # type: ignore
