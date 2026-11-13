import sqlite3
from contextlib import closing

import pytest

import inlay
from inlay import Ref, SchemaMismatch, entity, key


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


def test_open_creates_tables_and_indexes(tmp_path):
    path = tmp_path / "shop.db"
    with inlay.open([Customer, Order, Category], path=path):
        pass
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


def test_reopen_with_same_entities(tmp_path):
    path = tmp_path / "shop.db"
    inlay.open([Customer, Order], path=path).close()
    inlay.open([Customer, Order], path=path).close()


def test_open_without_a_path_uses_a_hidden_file_in_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inlay.open([Customer]).close()
    assert "customer" in schema(tmp_path / ".inlay.db")


def test_changed_class_raises_and_changes_nothing(tmp_path):
    path = tmp_path / "shop.db"
    inlay.open([Customer], path=path).close()

    renamed_field = entity(type("Customer", (), {"__annotations__": {"id": str, "full_name": str}, "id": key()}))

    with pytest.raises(SchemaMismatch, match="table 'customer' does not match its entity"):
        inlay.open([Category, renamed_field], path=path)
    assert "category" not in schema(path)


def test_ref_target_must_be_opened_too(tmp_path):
    with pytest.raises(TypeError, match="not passed to inlay.open: Order.customer -> Customer"):
        inlay.open([Order], path=tmp_path / "shop.db")
