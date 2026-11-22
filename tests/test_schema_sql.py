from inlay.sql import reprefix_index_sql


def test_reprefix_drops_an_index_and_creates_it_named_after_the_new_table():
    stored = 'CREATE INDEX "order__customer" ON "purchase" ("customer")'
    assert reprefix_index_sql("order__customer", stored, "order", "purchase") == (
        'DROP INDEX "order__customer"',
        'CREATE INDEX "purchase__customer" ON "purchase" ("customer")',
    )
