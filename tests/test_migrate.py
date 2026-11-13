import sqlite3
from dataclasses import replace

import pytest

import inlay
from inlay import BrokenRef, Db, Ref, SchemaMismatch, entity, key


@entity
class Customer:
    id: str = key()
    name: str


def order_line(order_class: type) -> type:
    """The class of the table order_line, with its references pointing to `order_class`."""

    @entity(indexes=[("order",)])
    class OrderLine:
        id: str = key()
        order: Ref[order_class]

    return OrderLine


def old_order() -> type:
    """The class of the table order as it was when the file was written."""

    @entity(indexes=[("customer",)])
    class Order:
        id: str = key()
        customer: Ref[Customer]
        total: int

    return Order


OldOrder = old_order()
OldLine = order_line(OldOrder)


# Case 1: the old shape under another name, and the new class with the old name.
@entity(indexes=[("customer",)])
class OrderLegacy:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int
    discount: int


OrderLine = order_line(Order)


# Case 2: the new class has a table name of its own.
@entity(indexes=[("customer",)])
class Purchase:
    id: str = key()
    customer: Ref[Customer]
    total: int
    discount: int


PurchaseLine = order_line(Purchase)


@entity(indexes=[("customer",)])
class Booking:
    id: str = key()
    customer: Ref[Customer]
    total: int


BookingLine = order_line(Booking)


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "shop.db"
    ana = Customer(name="Ana")
    order = OldOrder(id="o1", customer=ana.id, total=10)
    with inlay.open([Customer, OldOrder, OldLine], path=path) as db:
        db.add(ana, order, OldLine(id="l1", order=order.id))
    return path


def table_names(path):
    with sqlite3.connect(path) as conn:
        return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def index_names(path, table):
    with sqlite3.connect(path) as conn:
        query = "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL"
        return {name for (name,) in conn.execute(query, (table,))}


def add_discount(db: Db) -> None:
    db.add(*(Order(id=o.id, customer=o.customer, total=o.total, discount=0) for o in db[OrderLegacy]))


def add_discount_to_purchase(db: Db) -> None:
    db.add(*(Purchase(id=o.id, customer=o.customer, total=o.total, discount=0) for o in db[OldOrder]))


def test_the_new_class_takes_the_name_of_the_old_one(path):
    inlay.migrate(add_discount, OrderLegacy, Order, path=path)
    assert table_names(path) == {"customer", "order", "order_line"}
    with inlay.open([Customer, Order, OrderLine], path=path) as db:
        (order,) = db[Order]
        assert (order.id, order.total, order.discount) == ("o1", 10, 0)
        assert [line.order for line in db[OrderLine]] == ["o1"]


def test_the_indexes_of_the_new_class_exist_after_the_old_table_is_gone(path):
    inlay.migrate(add_discount, OrderLegacy, Order, path=path)
    assert index_names(path, "order") == {"order__customer"}


def test_the_new_class_has_a_table_of_its_own_and_the_references_to_the_old_one_follow(path):
    inlay.migrate(add_discount_to_purchase, OldOrder, Purchase, [PurchaseLine], path=path)
    assert table_names(path) == {"customer", "purchase", "order_line"}
    assert index_names(path, "order_line") == {"order_line__order"}
    # Opening checks the stored table of the lines against a class that refers to Purchase.
    with inlay.open([Customer, Purchase, PurchaseLine], path=path) as db:
        (purchase,) = db[Purchase]
        assert [line.order for line in db[PurchaseLine]] == [purchase.id]


def test_fn_can_change_keys_and_update_the_records_that_refer_to_them(path):
    def rekey(db: Db) -> None:
        db.add(*(Booking(id="b-" + o.id, customer=o.customer, total=o.total) for o in db[OldOrder]))
        db.update(*(replace(line, order="b-" + line.order) for line in tuple(db[BookingLine])))

    inlay.migrate(rekey, OldOrder, Booking, [BookingLine], path=path)
    with inlay.open([Customer, Booking, BookingLine], path=path) as db:
        assert [b.id for b in db[Booking]] == ["b-o1"]
        assert [line.order for line in db[BookingLine]] == ["b-o1"]


def test_running_it_again_does_nothing(path):
    calls = []
    inlay.migrate(lambda db: (calls.append(1), add_discount(db)), OrderLegacy, Order, path=path)
    inlay.migrate(lambda db: calls.append(1), OrderLegacy, Order, path=path)
    assert calls == [1]


@pytest.mark.parametrize(
    "old, new, fn",
    [(OrderLegacy, Order, add_discount), (OldOrder, Purchase, add_discount_to_purchase)],
)
def test_an_error_in_fn_changes_nothing(path, old, new, fn):
    def failing(db: Db) -> None:
        fn(db)
        raise RuntimeError

    with pytest.raises(RuntimeError):
        inlay.migrate(failing, old, new, path=path)
    assert table_names(path) == {"customer", "order", "order_line"}
    with inlay.open([Customer, OldOrder, OldLine], path=path) as db:
        assert len(db[OldOrder]) == 1


def test_a_reference_to_nothing_changes_nothing(path):
    def broken(db: Db) -> None:
        add_discount_to_purchase(db)
        db.add(Purchase(customer="nobody", total=1, discount=0))

    with pytest.raises(BrokenRef, match="not stored"):
        inlay.migrate(broken, OldOrder, Purchase, path=path)
    assert table_names(path) == {"customer", "order", "order_line"}


def test_the_old_and_the_new_class_cannot_share_a_table(path):
    with pytest.raises(ValueError, match="use the same table"):
        inlay.migrate(add_discount, OldOrder, Order, path=path)


def test_a_db_without_the_old_table_is_refused(tmp_path):
    path = tmp_path / "other.db"
    with inlay.open([Customer], path=path):
        pass
    with pytest.raises(SchemaMismatch, match="cannot migrate Order to Purchase"):
        inlay.migrate(add_discount_to_purchase, OldOrder, Purchase, path=path)


def old_category() -> type:
    """The class of the table category as it was when the file was written."""

    @entity(indexes=[("parent",)])
    class Category:
        id: str = key()
        parent: Ref[Category]
        name: str

    return Category


@entity(indexes=[("parent",)])
class CategoryLegacy:
    id: str = key()
    parent: Ref[CategoryLegacy]
    name: str


@entity(indexes=[("parent",)])
class Category:
    id: str = key()
    parent: Ref[Category]
    name: str
    top_level: bool


def test_a_class_that_refers_to_itself_is_migrated_with_its_references(tmp_path):
    path = tmp_path / "shop.db"
    old = old_category()
    with inlay.open([old], path=path) as db:
        db.add(old(id="all", parent="all", name="All"), old(id="books", parent="all", name="Books"))
        db.add(old(id="novels", parent="books", name="Novels"))

    def add_top_level(db: Db) -> None:
        # The reference to itself leads to the old records while the migration runs.
        top = {c.id for c in db[CategoryLegacy].filter(CategoryLegacy.parent.name == "All")}
        db.add(*(Category(id=c.id, parent=c.parent, name=c.name, top_level=c.id in top) for c in db[CategoryLegacy]))

    inlay.migrate(add_top_level, CategoryLegacy, Category, path=path)
    with inlay.open([Category], path=path) as db:
        assert {c.id: (c.parent, c.top_level) for c in db[Category]} == {
            "all": ("all", True),
            "books": ("all", True),
            "novels": ("books", False),
        }
