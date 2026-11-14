from inlay import Ref, entity, key
from inlay.history.sql import create_index_sql, create_table_sql, version_index_sqls
from inlay.meta import meta

CURRENT = "9223372036854775807"


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
        '"_tx_from" INTEGER NOT NULL, "_tx_to" INTEGER NOT NULL, '
        f'"_live_id" TEXT GENERATED ALWAYS AS (CASE WHEN "_tx_to" = {CURRENT} THEN "id" END) VIRTUAL UNIQUE, '
        f'"_live_customer" TEXT GENERATED ALWAYS AS (CASE WHEN "_tx_to" = {CURRENT} THEN "customer" END) VIRTUAL '
        'REFERENCES "customer" ("_live_id") DEFERRABLE INITIALLY DEFERRED) STRICT'
    )


def test_indexes_start_with_tx_to_and_every_ref_gets_an_index_on_its_live_column():
    m = meta(Order)
    assert [create_index_sql(m, i) for i in m.indexes] == [
        'CREATE INDEX IF NOT EXISTS "order__customer" ON "order" ("_tx_to", "customer")',
        'CREATE INDEX IF NOT EXISTS "order__total" ON "order" ("_tx_to", "total")',
    ]
    assert version_index_sqls(m) == [
        'CREATE INDEX IF NOT EXISTS "order___tx_to___tx_from" ON "order" ("_tx_to", "_tx_from")',
        'CREATE INDEX IF NOT EXISTS "order__id___tx_to" ON "order" ("id", "_tx_to")',
        'CREATE INDEX IF NOT EXISTS "order___live_customer" ON "order" ("_live_customer") '
        'WHERE "_live_customer" IS NOT NULL',
    ]
