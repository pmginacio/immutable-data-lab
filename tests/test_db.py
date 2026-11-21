import sqlite3
from contextlib import closing

import pytest

import inlay
import inlay.history
from inlay import BrokenRef, Ref, SchemaMismatch, entity, key


@entity(indexes=[("city", "name")])
class Customer:
    id: str = key()
    name: str
    city: str


@entity(indexes=[("customer",), ("total",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity(indexes=[("parent",)])
class Category:
    id: str = key()
    parent: Ref[Category]
    name: str


def schema(path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as conn:
        return dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def test_register_creates_tables_and_indexes(tmp_path):
    path = tmp_path / "shop.db"
    with inlay.open(path) as db:
        db.register(Customer, Order, Category)
    stored = schema(path)
    assert set(stored) == {
        "customer",
        "customer__city__name",
        "order",
        "order__customer",
        "order__total",
        "category",
        "category__parent",
    }
    assert stored["order"] == (
        'CREATE TABLE "order" ("id" TEXT NOT NULL PRIMARY KEY, '
        '"customer" TEXT NOT NULL REFERENCES "customer" ("id") DEFERRABLE INITIALLY DEFERRED, '
        '"total" INTEGER NOT NULL) STRICT'
    )
    assert stored["customer__city__name"] == 'CREATE INDEX "customer__city__name" ON "customer" ("city", "name")'
    assert stored["order__customer"] == 'CREATE INDEX "order__customer" ON "order" ("customer")'
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


# These also run against the history db; the datoms version, which is paused, still takes its classes in `open`.
@pytest.fixture(params=[inlay.open, inlay.history.open], ids=["inlay", "history"])
def open_db(request):
    return request.param


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shop.db"


def test_a_registered_class_is_there_when_the_file_is_opened_again(open_db, path):
    ana = Customer(name="Ana", city="Lisbon")
    with open_db(path) as db:
        db.register(Customer)
        db.add(ana)
    with open_db(path) as db:
        assert Customer in db
        assert Order not in db
        assert list(db[Customer]) == [ana]


def test_open_without_a_path_uses_a_hidden_file_in_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with inlay.open() as db:
        db.register(Customer)
    assert "customer" in schema(tmp_path / ".inlay.db")


def test_register_raises_for_a_class_with_a_table(open_db, path):
    with open_db(path) as db:
        db.register(Customer)
        with pytest.raises(SchemaMismatch, match="a table is already in this db for Customer"):
            db.register(Customer)


def test_register_needs_the_targets_of_refs_first_or_in_the_same_call(open_db, path):
    with open_db(path) as db:
        with pytest.raises(TypeError, match="Customer must be registered first"):
            db.register(Order)
        assert Order not in db
        db.register(Customer)
        db.register(Order, Category)


@entity(indexes=[("manager",)])
class Department:
    id: str = key()
    manager: Ref[Employee]


@entity(indexes=[("department",)])
class Employee:
    id: str = key()
    department: Ref[Department]


def test_classes_that_point_to_each_other_are_registered_in_one_call(open_db, path):
    with open_db(path) as db:
        db.register(Department, Employee)
        db.add(Department(id="d", manager="e"), Employee(id="e", department="d"))
        assert [e.id for e in db[Employee]] == ["e"]


def another_order() -> type:
    @entity
    class Order:
        id: str = key()

    return Order


def test_two_classes_with_the_same_table_raise(open_db, path):
    with open_db(path) as db, pytest.raises(TypeError, match="Order and .*Order use the same table"):
        db.register(Order, another_order())


def test_a_changed_class_raises_on_first_use(open_db, path):
    with open_db(path) as db:
        db.register(Customer)
    renamed_field = entity(type("Customer", (), {"__annotations__": {"id": str, "full_name": str}, "id": key()}))
    with open_db(path) as db:
        assert renamed_field in db
        with pytest.raises(SchemaMismatch, match="table 'customer' does not match its entity"):
            db[renamed_field]


def test_a_register_that_is_rolled_back_leaves_no_table(open_db, path):
    with open_db(path) as db:
        with pytest.raises(RuntimeError), db.transaction():
            db.register(Customer)
            db.add(Customer(name="Ana", city="Lisbon"))
            raise RuntimeError
        assert Customer not in db
        with pytest.raises(KeyError, match="no table in this db for Customer"):
            db[Customer]


def test_del_drops_the_table_unless_another_points_to_it(open_db, path):
    with open_db(path) as db:
        db.register(Customer, Order)
        with pytest.raises(BrokenRef, match="tables \\['order'\\] point to it"):
            del db[Customer]
        del db[Order]
        del db[Customer]
        assert Customer not in db
        with pytest.raises(KeyError, match="no table in this db for Customer"):
            del db[Customer]


def test_the_indexes_follow_the_class_on_first_use(open_db, path):
    with open_db(path) as db:
        db.register(Customer)
    fewer = entity(indexes=[])(
        type("Customer", (), {"__annotations__": {"id": str, "name": str, "city": str}, "id": key()})
    )
    with open_db(path) as db:
        db[fewer]
    assert "customer__city__name" not in schema(path)
    with open_db(path) as db:
        db[Customer]
    assert "customer__city__name" in schema(path)
