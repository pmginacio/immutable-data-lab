from inlay import Ref, entity, key
from inlay.history.sql import (
    clear_sql,
    create_index_sql,
    create_table_sql,
    delete_by_key_sql,
    delete_equal_sql,
    insert_sql,
    version_index_sqls,
)
from inlay.meta import meta

CURRENT = "9223372036854775807"
TX = '(SELECT max("tx") FROM "inlay_tx")'


@entity
class Customer:
    id: str = key()
    name: str


@entity(indexes=[("customer",), ("total",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


def test_a_table_holds_versions_and_live_columns_for_the_key_and_references():
    assert create_table_sql(meta(Order)) == (
        'CREATE TABLE "order" ("id" TEXT NOT NULL, "customer" TEXT NOT NULL, "total" INTEGER NOT NULL, '
        '"tx_from" INTEGER NOT NULL, "tx_to" INTEGER NOT NULL, '
        f'"live_id" TEXT GENERATED ALWAYS AS (CASE WHEN "tx_to" = {CURRENT} THEN "id" END) VIRTUAL UNIQUE, '
        f'"live_customer" TEXT GENERATED ALWAYS AS (CASE WHEN "tx_to" = {CURRENT} THEN "customer" END) VIRTUAL '
        'REFERENCES "customer" ("live_id") DEFERRABLE INITIALLY DEFERRED) STRICT'
    )


def test_indexes_start_with_tx_to_and_every_ref_gets_an_index_on_its_live_column():
    m = meta(Order)
    assert [create_index_sql(m, i) for i in m.indexes] == [
        'CREATE INDEX IF NOT EXISTS "order__customer" ON "order" ("tx_to", "customer")',
        'CREATE INDEX IF NOT EXISTS "order__total" ON "order" ("tx_to", "total")',
    ]
    assert version_index_sqls(m) == [
        'CREATE INDEX IF NOT EXISTS "order__tx_to__tx_from" ON "order" ("tx_to", "tx_from")',
        'CREATE INDEX IF NOT EXISTS "order__id__tx_from" ON "order" ("id", "tx_from")',
        'CREATE INDEX IF NOT EXISTS "order__live_customer" ON "order" ("live_customer")',
    ]


def test_writes_add_versions_and_end_current_ones():
    m = meta(Customer)
    assert insert_sql(m) == (
        f'INSERT INTO "customer" ("id", "name", "tx_from", "tx_to") VALUES (?, ?, {TX}, {CURRENT})'
    )
    assert delete_by_key_sql(m) == f'UPDATE "customer" SET "tx_to" = {TX} WHERE "tx_to" = {CURRENT} AND "id" = ?'
    assert delete_equal_sql(m) == (
        f'UPDATE "customer" SET "tx_to" = {TX} WHERE "tx_to" = {CURRENT} AND "id" = ? AND "name" = ?'
    )
    assert clear_sql(m, Customer.name == "Ana") == (
        f'UPDATE "customer" SET "tx_to" = {TX} WHERE "tx_to" = {CURRENT} AND "name" = ?',
        ["Ana"],
    )
