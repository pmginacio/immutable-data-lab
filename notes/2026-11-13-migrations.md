# Migrations

Notes from a design discussion on 2026-11-13.
Nothing here is decided or built yet.
The README lists migrations as out of scope for version one.

The last section, "Current direction", is where the discussion ended up.
It replaces the `migrations=[fn]` and `db.previous` parts above, and keeps the notes on renames and foreign keys.

## How it should feel

First imagine how a user wants to write a migration, then design the implementation for it.

A migration is a plain Python function that takes the db and returns nothing.
It reads the old records and adds the new ones.
There is no SQL, and no copy of the old class to keep around.

```python
def add_placed(db: Db) -> None:
    for order in db.previous(Order):
        db.add(Order(**vars(order), placed=date(2026, 1, 1)))

with inlay.open("shop.db", [Customer, Order], migrations=[add_placed]) as db:
    ...
```

This is the usual Python way to change a collection, `new = {f(x) for x in old}`, run inside the db.

## How `upgrade(db) -> None` could work

During a migration each class has two sets: the records as they were and the records as they will be.

- `db.previous(Order)` gives the old records, as frozen values with the old fields.
  inlay builds them from the shape it stored for the class, for example with `dataclasses.make_dataclass`, so the user never writes the old class.
- `db.add`, `db[Order]` and the rest work on the new set.
- Classes whose shape did not change carry over by themselves.
- When the function returns, the old sets are dropped and references are checked.
  If it raises, nothing changes, as with a transaction.

## Running migrations from `open`

Migrations run inside `open`, not on an open db.
Today `open` refuses a file that does not match the classes, and a `Db` has a fixed set of classes; a `Db` that changed its classes halfway would leave held views pointing at shapes that are gone.

A migration does not say which shape it starts from, so the schema cannot serve as the version.
Instead the db records which migrations have run, by name, and `open` runs the rest in list order.
A new file gets the latest classes, and every migration is marked as done.
After the migrations, the stored shapes must match the classes, or `open` raises `SchemaMismatch`.

## The hard part: old migrations and new classes

A migration is written against today's classes.
If `Order` changes again later, an old migration builds an `Order` that no longer fits.
Django solves this by keeping a historical copy of every model, which is a lot of machinery.

The simple answer is that migrations are short-lived: once every db file has run one, it is deleted.
A file older than the oldest migration gets a clear error.
This is a real trade-off and is not decided.

## Renaming a class

A rename is information only the user has.
From the outside, "Purchase was Order" looks the same as "Order was deleted and Purchase is new", so the user has to say it somewhere.

Two ways were considered.

The first decouples the table name from the class name with a catalog.
inlay keeps a table of its own with one row per class: the class name, the table that holds its records, and its shape.
`open` looks a class up by `__name__`, so moving a class to another module is not a rename.
A rename only changes the name in the catalog, and the table keeps its old name.

The second keeps "table name = class name" as the only rule.
`db.rename("Order", Purchase)` in a migration renames the table too.
In SQLite this is `ALTER TABLE ... RENAME`, which changes only the schema and also updates the `REFERENCES` in other tables.

The second was preferred.
The catalog does not remove the need to declare the rename, so it adds a lookup without saving the user anything.
After a few renames a catalog would leave `Purchase` in table `order` and a new `Order` in `order_2`, which is confusing in a SQLite shell.
The `table=` parameter of `@entity` could then go, leaving one way to name a table.

A catalog may still be useful later for other reasons: storing each class's shape for `db.previous`, and recording which migrations have run.
It can be keyed by class name, since that is also the table name.

## Renames must not look like lost data

Today, if a user renames `A` to `B` across the code and opens the db with `[B]`, `open` creates an empty table `b`.
`db[B]` is empty and nothing says why; the data is still in table `a`.
`open` only checks the tables of the classes it is given and ignores tables no class claims.

The fix is to make `open` strict in that direction too: the classes passed to `open` must account for every table in the file.

```
SchemaMismatch: table 'a' has no class among those passed to open.
If A was renamed, add a migration that calls db.rename("A", B). If it was deleted, one that calls db.drop("A").
```

When a new class has the same fields as a table no class claims, the message can ask "was A renamed to B?".
It only suggests it, because two classes with the same fields can be different things.

The cost is that a file can no longer be opened with only some of its classes.
That seems right: a db file belongs to one set of classes, as a pickle belongs to its types.
`open` already requires every class a `Ref` points to.

## Foreign keys

inlay does use foreign keys.
Every `Ref` column is created with `REFERENCES ... DEFERRABLE INITIALLY DEFERRED` and `PRAGMA foreign_keys = ON`, which is what raises `BrokenRef`.
"No cascading deletes" means deleting a customer with orders raises instead of deleting the orders.
A backend may have to turn the constraints off while it swaps tables, but the user only sees "references are checked when the migration ends".

## Open questions

- Is a strict `open`, which refuses tables without a class, the right default?
- Should renames come first, as a step of their own: the strict check, `db.rename` and `migrations=` on `open`?
- Are short-lived migrations acceptable, or do old migrations need to keep working?
- Should `previous` take the class, the class name, or both, given that a renamed class has no old class to pass?

## Current direction

Later in the same discussion the design moved to explicit old classes and a standalone call, outside `open`.

```python
inlay.migrate("shop.db", upgrade, A_legacy, A)    # case 1
inlay.migrate("shop.db", upgrade, A_v1, A_v2)     # case 2
inlay.rename("shop.db", "A", "B")                 # a pure rename, no shape change
```

A migration is a schema change of one class.
A bigger change is several calls, and each call can be run again safely.
A change that does not alter the schema is a normal db operation, not a migration.
The user keeps the old class in the code while the migration exists.
Nothing is generated from the stored shape.

### The two cases

- Case 1: the old class is renamed in code (`A_legacy`) and the new class takes the old name (`A`).
  The table named after the new class holds the old shape, so `migrate` renames it to the old class's table name, and creates the new table.
- Case 2: the old class keeps its name (`A_v1`) and the new class has a new name (`A_v2`).
  The new table does not exist yet, so `migrate` creates it.

In both cases the old table is dropped at the end.
There is no option to keep it, because copying a class to another name is not a use case.

After case 1 has moved the old table aside, both cases are in the same state: the old table exists under the old class's table name, and the new table does not exist.
Everything after that step is shared.

### What `migrate` does

0. Before connecting, raise `ValueError` if the old and the new class use the same table name.
1. Open the file in read-write mode, so a wrong path never creates an empty file.
   Turn foreign keys off and `legacy_alter_table` on. `PRAGMA foreign_keys` does nothing inside a transaction, so this comes first.
2. `BEGIN IMMEDIATE`.
3. If the new class's table already has the new shape, commit and return: the migration is done.
4. If the new class's table holds the old shape (case 1), rename it to the old class's table name.
5. Now both cases look the same.
   Raise `SchemaMismatch` unless the old table exists with the old shape and the new table does not exist.
6. Create the new table.
7. Call `upgrade(db)`.
   It reads the old class and writes the new one, and it can use any class passed in `other_entities`.
8. Re-point the foreign keys that referred to the old table, and drop the old table.
9. Create the indexes of the new class.
   They come last because a renamed table keeps its index names, which would make `CREATE INDEX IF NOT EXISTS` skip an index of the new class and lose it when the old table is dropped.
   It also makes the copy faster.
10. Check that every class passed matches its table, except the old one, and run `PRAGMA foreign_key_check`, which is where `BrokenRef` comes from.
11. Commit. If anything raises, roll back everything and re-raise.

The connection belongs to `migrate` and is closed at the end, so the pragmas never leak.
Calling it at every startup is safe, and two processes queue on the write lock.

### References

The old and the new class are all `migrate` needs to know about references.
Every table in the file with a foreign key to the old table must end up pointing to the new one.
`migrate` finds those tables with `PRAGMA foreign_key_list`, so it needs no class for them, and the step runs in both cases.
It rebuilds each one in SQL: create, copy the rows as stored, drop, rename, and recreate its indexes.
In case 1 the move-aside runs with foreign keys off and `legacy_alter_table` on, so the other tables still say `REFERENCES` the new table's name, nothing refers to the old table, and the step does nothing.
In case 2 the rebuild is needed, because the new table is created and filled, not renamed, and SQLite rewrites nothing for that.
If `upgrade` changed keys, it updates those rows itself.

### `rename`

`rename` takes class names and changes only the table name.
It turns them into table names with the same rule as `@entity`.
It takes no class, because the old class is gone from the code.
It runs in one transaction with foreign keys on, so SQLite itself rewrites the `REFERENCES` in the other tables.
SQLite cannot rename an index, so it drops and recreates the table's indexes under the new prefix.
It is not idempotent: it raises `SchemaMismatch` if the old table does not exist or the new one already does.
A wrong rename also shows up as `SchemaMismatch` at the next `open` or `migrate`.

### `other_entities`

`upgrade` needs the class object of any class it reads or writes, because `db[Customer]` works only for classes the db knows.
A class it does not touch need not be listed, and the argument defaults to empty.
These are the ordinary classes of the app, not copies.
The list exists only because a class has to be passed in.
If a class were set up the first time `db[T]` uses it, as in the follow-up on not passing entities to `open`, the list would go away.

### Implementation

`meta.py`:

- `table_name(class_name)` is the snake-casing, moved out of `entity.py`, so `@entity` and `rename` share one rule.

`sql.py`, pure functions that return SQL text:

- `MIGRATION_SETUP`, the two pragmas above, and `FOREIGN_KEY_CHECK`.
- `REFERRERS`, a query for the tables that have a foreign key to a given table, using `pragma_foreign_key_list`.
- `LIST_INDEXES`, the name and SQL of each index of a table.
- `rename_table_sql(old, new)` and `drop_table_sql(table)`.
- `repoint_sql(table, stored_sql, old, new)`, which replaces `REFERENCES "old" ("old_key")` with the new table and key in a stored `CREATE TABLE`, and returns the statements that create a staging table, copy the rows, drop the old table and rename the staging table.
  It works on the stored text because `_column_def` writes the clause in a fixed format.
- `reprefix_index_sql(name, sql, old_table, new_table)`, which returns the drop and the create of an index under its new name.

`db.py`:

- `migrate(path, fn, old, new, other_entities=())` and `rename(path, old, new)`.
- `_connect(path, create)`, shared with `open`, where `create=False` opens in read-write mode only.
- `_repoint_referrers(conn, old, new)`, which finds the referrers, reads their indexes, runs `repoint_sql`, and recreates the indexes.
- `_check_foreign_keys(conn)`, which runs `FOREIGN_KEY_CHECK` and raises `BrokenRef`.
- It reuses `_check_tables`, `_execute_all`, `Db`, `create_table_sql` and `create_index_sql`.
  `fn` gets a `Db` over `migrate`'s own connection, and writes inside it use savepoints because a transaction is already open.

`__init__.py` exports `migrate` and `rename`.

### Tests

- `repoint_sql`, `reprefix_index_sql` and `table_name` as plain strings, without SQLite.
- `migrate`: case 1, case 2, referrers re-pointed, re-keying in `upgrade`, running it again, rollback when `upgrade` raises, rollback on `BrokenRef`, and a missing file not being created.
- `rename`: the table name, the referrers' stored SQL, the index names, and the two errors.

### Commits

1. Move the snake-casing to `meta.py` and add `_connect`.
2. The SQL builders and their tests.
3. `rename` and its tests.
4. `migrate` and its tests.
5. The README, and the notes.

### Checked and not built

- Checked in a scratch test: with foreign keys on, SQLite rewrites the references in other tables when a table is renamed.
  With foreign keys off and `legacy_alter_table` on, it keeps them, and `foreign_key_check` is clean after the swap and the drop.
- Not built or tested: everything under Implementation.
- Still to check at the start: that `pragma_foreign_key_list` works as a table-valued function in the bundled SQLite, and the exact `CREATE INDEX` text that SQLite stores.

### Out of scope

- Detecting a `rename` that has already happened, for apps that migrate many db files at startup.
  The narrow check in `open`, which raises when it is about to create a table and an unclaimed table has the same columns, would make that possible.
- `open` does not change.

### Open questions

- Is `del db[Cls]` still needed to drop a table, now that `migrate` always drops the old one?
