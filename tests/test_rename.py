import sqlite3

import pytest

import inlay
from inlay import Ref, SchemaMismatch, entity, key


@entity
class Customer:
    id: str = key()
    name: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity(indexes=[("customer",)])
class Purchase:
    id: str = key()
    customer: Ref[Customer]
    total: int


def order_line(order_class: type) -> type:
    """The class of the table order_line, with its references pointing to `order_class`."""

    @entity(indexes=[("order",)])
    class OrderLine:
        id: str = key()
        order: Ref[order_class]

    return OrderLine


OrderLine = order_line(Order)
PurchaseLine = order_line(Purchase)


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "shop.db"
    ana = Customer(name="Ana")
    order = Order(customer=ana.id, total=10)
    with inlay.open([Customer, Order, OrderLine], path=path) as db:
        db.add(ana, order, OrderLine(order=order.id))
    return path


def test_rename_keeps_the_data_and_the_references_of_other_tables(path):
    inlay.rename("Order", "Purchase", path=path)
    # Opening checks the stored table of the lines against a class that refers to Purchase.
    with inlay.open([Customer, Purchase, PurchaseLine], path=path) as db:
        (purchase,) = db[Purchase]
        (line,) = db[PurchaseLine]
        assert (purchase.total, line.order) == (10, purchase.id)


def test_rename_renames_the_indexes_of_the_table(path):
    inlay.rename("Order", "Purchase", path=path)
    with sqlite3.connect(path) as conn:
        names = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'purchase' AND sql IS NOT NULL"
        )
        assert [name for (name,) in names] == ["purchase__customer"]


def test_rename_is_not_idempotent(path):
    inlay.rename("Order", "Purchase", path=path)
    with pytest.raises(SchemaMismatch, match="no table 'order'"):
        inlay.rename("Order", "Purchase", path=path)


def test_rename_onto_an_existing_table_fails_and_changes_nothing(path):
    with pytest.raises(SchemaMismatch, match="table 'customer' already exists"):
        inlay.rename("Order", "Customer", path=path)
    with inlay.open([Customer, Order, OrderLine], path=path) as db:
        assert len(db[Order]) == 1


def test_rename_does_not_create_a_missing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        inlay.rename("Order", "Purchase", path=tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()
