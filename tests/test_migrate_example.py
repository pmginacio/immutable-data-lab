"""The migration shown in the README: a new field computed from the records of another class."""

import inlay
from inlay import Db, Ref, entity, key


@entity
class Customer:
    id: str = key()
    name: str


# The classes as they were when the file was written.
@entity(table="order", indexes=[("customer",)])
class OrderBefore:
    id: str = key()
    customer: Ref[Customer]


@entity(table="line_item", indexes=[("order",)])
class LineItemBefore:
    id: str = key()
    order: Ref[OrderBefore]
    price: int
    qty: int


@entity(indexes=[("customer",)])
class OrderLegacy:
    id: str = key()
    customer: Ref[Customer]


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total_amount: int


@entity(indexes=[("order",)])
class LineItem:
    id: str = key()
    order: Ref[Order]
    price: int
    qty: int


def add_total_amount(db: Db) -> None:
    for old in db[OrderLegacy]:
        lines = db[LineItem].filter(LineItem.order == old.id)
        db.add(Order(id=old.id, customer=old.customer, total_amount=lines.sum(LineItem.price * LineItem.qty)))


def test_a_field_can_be_computed_from_the_records_of_another_class(tmp_path):
    path = tmp_path / "shop.db"
    ana = Customer(name="Ana")
    with inlay.open(path, [Customer, OrderBefore, LineItemBefore]) as db:
        db.add(ana, OrderBefore(id="o1", customer=ana.id), OrderBefore(id="o2", customer=ana.id))
        db.add(LineItemBefore(order="o1", price=10, qty=2), LineItemBefore(order="o1", price=5, qty=1))

    inlay.migrate(path, add_total_amount, OrderLegacy, Order, other_entities=[LineItem])

    with inlay.open(path, [Customer, Order, LineItem]) as db:
        assert {o.id: o.total_amount for o in db[Order]} == {"o1": 25, "o2": 0}
        assert len(db[LineItem]) == 2
