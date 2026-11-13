"""The migration shown in the README: a new field computed from the records of another class."""

import inlay
from inlay import Db, Ref, entity, key


@entity
class Customer:
    id: str = key()
    name: str


def before() -> tuple[type, type]:
    """The classes as they were when the file was written."""

    @entity(indexes=[("customer",)])
    class Order:
        id: str = key()
        customer: Ref[Customer]

    @entity(indexes=[("order",)])
    class LineItem:
        id: str = key()
        order: Ref[Order]
        price: int
        qty: int

    return Order, LineItem


OrderBefore, LineItemBefore = before()


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
    with inlay.open([Customer, OrderBefore, LineItemBefore], path=path) as db:
        db.add(ana, OrderBefore(id="o1", customer=ana.id), OrderBefore(id="o2", customer=ana.id))
        db.add(LineItemBefore(order="o1", price=10, qty=2), LineItemBefore(order="o1", price=5, qty=1))

    inlay.migrate(add_total_amount, OrderLegacy, Order, other_entities=[LineItem], path=path)

    with inlay.open([Customer, Order, LineItem], path=path) as db:
        assert {o.id: o.total_amount for o in db[Order]} == {"o1": 25, "o2": 0}
        assert len(db[LineItem]) == 2
