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


# The old shape under another name, and the new class with the name of the table.
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


def renumbered_order() -> type:
    """Order with a key of another name, which the table order_line still points to."""

    @entity(indexes=[("customer",)])
    class Order:
        number: str = key()
        customer: Ref[Customer]
        total: int

    return Order


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "shop.db"
    ana = Customer(name="Ana")
    order = OldOrder(id="o1", customer=ana.id, total=10)
    with inlay.open(path) as db:
        db.register(Customer, OldOrder, OldLine)
        db.add(ana, order, OldLine(id="l1", order=order.id))
    return path


def migrate(path, fn, old, new):
    with inlay.open(path) as db:
        db.migrate(fn, old, new)


def table_names(path):
    with sqlite3.connect(path) as conn:
        return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def index_names(path, table):
    with sqlite3.connect(path) as conn:
        query = "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL"
        return {name for (name,) in conn.execute(query, (table,))}


def add_discount(db: Db) -> None:
    db.add(*(Order(id=o.id, customer=o.customer, total=o.total, discount=0) for o in db[OrderLegacy]))


def test_the_new_class_takes_the_name_of_the_old_one(path):
    migrate(path, add_discount, OrderLegacy, Order)
    assert table_names(path) == {"customer", "order", "order_line"}
    with inlay.open(path) as db:
        (order,) = db[Order]
        assert (order.id, order.total, order.discount) == ("o1", 10, 0)
        assert [line.order for line in db[OrderLine]] == ["o1"]


def test_the_indexes_of_the_new_class_exist_after_the_old_table_is_gone(path):
    migrate(path, add_discount, OrderLegacy, Order)
    assert index_names(path, "order") == {"order__customer"}


def test_fn_can_change_keys_and_update_the_records_that_refer_to_them(path):
    def rekey(db: Db) -> None:
        db.add(*(Order(id="b-" + o.id, customer=o.customer, total=o.total, discount=0) for o in db[OrderLegacy]))
        db.update(*(replace(line, order="b-" + line.order) for line in tuple(db[OrderLine])))

    migrate(path, rekey, OrderLegacy, Order)
    with inlay.open(path) as db:
        assert [o.id for o in db[Order]] == ["b-o1"]
        assert [line.order for line in db[OrderLine]] == ["b-o1"]


def test_running_it_again_does_nothing(path):
    calls = []
    migrate(path, lambda db: (calls.append(1), add_discount(db)), OrderLegacy, Order)
    migrate(path, lambda db: calls.append(1), OrderLegacy, Order)
    assert calls == [1]


def test_an_error_in_fn_changes_nothing(path):
    def failing(db: Db) -> None:
        add_discount(db)
        raise RuntimeError

    with pytest.raises(RuntimeError):
        migrate(path, failing, OrderLegacy, Order)
    assert table_names(path) == {"customer", "order", "order_line"}
    with inlay.open(path) as db:
        assert len(db[OldOrder]) == 1


def test_a_reference_to_nothing_changes_nothing(path):
    def broken(db: Db) -> None:
        add_discount(db)
        db.add(Order(customer="nobody", total=1, discount=0))

    with pytest.raises(BrokenRef, match="not stored"):
        migrate(path, broken, OrderLegacy, Order)
    assert table_names(path) == {"customer", "order", "order_line"}


def test_classes_used_before_are_checked_again_after_it(path):
    with inlay.open(path) as db:
        len(db[OldOrder])
        db.migrate(add_discount, OrderLegacy, Order)
        with pytest.raises(SchemaMismatch, match="'order' does not match"):
            db[OldOrder]


def test_the_old_and_the_new_class_cannot_share_a_table(path):
    with pytest.raises(ValueError, match="use the same table"):
        migrate(path, add_discount, OldOrder, Order)


def test_the_table_keeps_its_name(path):
    @entity(indexes=[("customer",)])
    class Purchase:
        id: str = key()
        customer: Ref[Customer]
        total: int

    with pytest.raises(SchemaMismatch, match="rename changes it"):
        migrate(path, lambda db: None, OldOrder, Purchase)


def test_a_db_without_the_old_table_is_refused(tmp_path):
    path = tmp_path / "other.db"
    with inlay.open(path) as db:
        db.register(Customer)
    with pytest.raises(SchemaMismatch, match="cannot migrate OrderLegacy to Order"):
        migrate(path, add_discount, OrderLegacy, Order)


def test_the_key_cannot_change_while_other_tables_point_to_it(path):
    with pytest.raises(SchemaMismatch, match=r"the key changes, and tables \['order_line'\] point to it"):
        migrate(path, lambda db: None, OrderLegacy, renumbered_order())
    assert table_names(path) == {"customer", "order", "order_line"}


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
    with inlay.open(path) as db:
        db.register(old)
        db.add(old(id="all", parent="all", name="All"), old(id="books", parent="all", name="Books"))
        db.add(old(id="novels", parent="books", name="Novels"))

    def add_top_level(db: Db) -> None:
        # The reference to itself leads to the old records while the migration runs.
        top = {c.id for c in db[CategoryLegacy].filter(CategoryLegacy.parent.name == "All")}
        db.add(*(Category(id=c.id, parent=c.parent, name=c.name, top_level=c.id in top) for c in db[CategoryLegacy]))

    migrate(path, add_top_level, CategoryLegacy, Category)
    with inlay.open(path) as db:
        assert {c.id: (c.parent, c.top_level) for c in db[Category]} == {
            "all": ("all", True),
            "books": ("all", True),
            "novels": ("books", False),
        }
