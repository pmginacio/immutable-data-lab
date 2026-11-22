"""What aggregate_by gives: a mapping read from the db."""

import pytest

import inlay
from inlay import Ref, entity, key


@entity
class Customer:
    id: str = key()
    city: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db") as db:
        db.register(Customer, Order)
        db.add(Customer(id="ana", city="Lisbon"), Customer(id="bob", city="Porto"))
        db.add(Order(id="o1", customer="ana", total=150), Order(id="o2", customer="ana", total=50))
        yield db


def test_every_read_runs_its_query_again(db):
    totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
    assert dict(totals) == {"ana": 200}
    db.add(Order(id="o3", customer="bob", total=30))
    assert (totals["bob"], len(totals), "bob" in totals) == (30, 2, True)
