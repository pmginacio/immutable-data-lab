import pytest

from inlay import Ref, entity, key
from inlay.meta import meta
from inlay.sql import create_table_sql, repoint_sql, reprefix_index_sql


@entity
class Order:
    id: str = key()


@entity
class Purchase:
    number: str = key()


@entity(indexes=[("order",)])
class OrderLine:
    id: str = key()
    order: Ref[Order]


def test_repoint_rebuilds_a_table_with_its_foreign_key_on_the_new_table_and_key():
    stored = create_table_sql(meta(OrderLine))
    assert repoint_sql("order_line", stored, meta(Order), meta(Purchase)) == (
        'CREATE TABLE "order_line__rebuild" ("id" TEXT NOT NULL PRIMARY KEY, '
        '"order" TEXT NOT NULL REFERENCES "purchase" ("number") DEFERRABLE INITIALLY DEFERRED) STRICT',
        'INSERT INTO "order_line__rebuild" SELECT * FROM "order_line"',
        'DROP TABLE "order_line"',
        'ALTER TABLE "order_line__rebuild" RENAME TO "order_line"',
    )


def test_repoint_rejects_a_table_that_does_not_refer_to_the_old_one():
    with pytest.raises(ValueError, match="no foreign key to 'purchase'"):
        repoint_sql("order_line", create_table_sql(meta(OrderLine)), meta(Purchase), meta(Order))


def test_reprefix_drops_an_index_and_creates_it_named_after_the_new_table():
    stored = 'CREATE INDEX "order__customer" ON "purchase" ("customer")'
    assert reprefix_index_sql("order__customer", stored, "order", "purchase") == (
        'DROP INDEX "order__customer"',
        'CREATE INDEX "purchase__customer" ON "purchase" ("customer")',
    )
