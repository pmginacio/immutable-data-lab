# Migrations

Notes from a design discussion on 2026-11-13.
Nothing here is decided or built yet.
The README lists migrations as out of scope for version one.

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
