import sqlite3

import pytest

import inlay
from inlay import Field, Ref, SchemaMismatch, entity, key


@entity
class Customer:
    id: Field[str] = key()
    name: Field[str]


@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]


@entity(indexes=[("customer",)])
class Purchase:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]


def order_line(order_class: type) -> type:
    """The class of the table order_line, with its references pointing to `order_class`."""

    @entity(indexes=[("order",)])
    class OrderLine:
        id: Field[str] = key()
        order: Ref[order_class]

    return OrderLine


@entity(indexes=[("customer",)])
class OrderLegacy:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]


OrderLine = order_line(Order)
PurchaseLine = order_line(Purchase)


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "shop.db"
    ana = Customer(name="Ana")
    order = Order(customer=ana.id, total=10)
    with inlay.open(path) as db:
        db.register(Customer, Order, OrderLine)
        db.add(ana, order, OrderLine(order=order.id))
    return path


def rename(path, old, new):
    with inlay.open(path) as db:
        db.rename(old, new)


def test_rename_keeps_the_data_and_the_references_of_other_tables(path):
    rename(path, "Order", Purchase)
    # The first use checks the stored table of the lines against a class that refers to Purchase.
    with inlay.open(path) as db:
        (purchase,) = db[Purchase]
        (line,) = db[PurchaseLine]
        assert (purchase.total, line.order) == (10, purchase.id)


def test_rename_renames_the_indexes_of_the_table(path):
    rename(path, "Order", Purchase)
    with sqlite3.connect(path) as conn:
        names = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'purchase' AND sql IS NOT NULL"
        )
        assert [name for (name,) in names] == ["purchase__customer"]


def test_running_it_again_does_nothing(path):
    rename(path, "Order", Purchase)
    rename(path, "Order", Purchase)
    with inlay.open(path) as db:
        assert len(db[Purchase]) == 1


def test_a_missing_table_is_refused(tmp_path):
    with pytest.raises(SchemaMismatch, match="no table 'order' to rename"):
        rename(tmp_path / "empty.db", "Order", Purchase)


def test_rename_onto_an_existing_table_fails_and_changes_nothing(path):
    with pytest.raises(SchemaMismatch, match="table 'customer' already exists"):
        rename(path, "Order", Customer)
    with inlay.open(path) as db:
        assert len(db[Order]) == 1


def test_the_renamed_table_must_match_the_new_class(path):
    @entity(indexes=[("customer",)])
    class Purchase:
        id: Field[str] = key()
        customer: Ref[Customer]
        total: Field[int]
        discount: Field[int]

    with pytest.raises(SchemaMismatch, match="'purchase' does not match"):
        rename(path, "Order", Purchase)
    with inlay.open(path) as db:
        assert len(db[Order]) == 1


def test_migrate_and_rename_in_one_transaction(path):
    @entity(indexes=[("customer",)])
    class Order:
        id: Field[str] = key()
        customer: Ref[Customer]
        total: Field[int]
        discount: Field[int]

    @entity(indexes=[("customer",)])
    class Purchase:
        id: Field[str] = key()
        customer: Ref[Customer]
        total: Field[int]
        discount: Field[int]

    def add_discount(db):
        db.add(*(Order(id=o.id, customer=o.customer, total=o.total, discount=0) for o in db[OrderLegacy]))

    with inlay.open(path) as db, db.transaction():
        db.migrate(add_discount, OrderLegacy, Order)
        db.rename("Order", Purchase)
    with inlay.open(path) as db:
        assert [p.discount for p in db[Purchase]] == [0]
