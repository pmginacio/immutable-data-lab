# Opening a db without a list of classes

A design for `open(path)` without the list of classes, decided on 2026-11-21.
Nothing is built yet.

## The problem

`open(entities, path=...)` takes every class the program will use, and the classes their `Ref`s point to.
Leaving one out raises "not passed to inlay.open", and `migrate` needs the same list as `other_entities`.
The list has to be kept in step with the code, in every script that opens the file.

## The model

The db is a mapping from classes to tables, and a table is a set of records that can also be looked up by key.
Each operation takes the name and the behaviour of the dict or set operation it matches, and only where the behaviour really matches.

| Database operation | SQL | dict or set idiom | inlay |
|---|---|---|---|
| create a table | `CREATE TABLE` | `d[k] = v` | `db.register(*classes)` |
| delete a table | `DROP TABLE` | `del d[k]` | `del db[Cls]` |
| does the table exist | `SELECT … FROM sqlite_master` | `k in d` | `Cls in db` |
| read a table | `SELECT … FROM t` | `d[k]`, raising `KeyError` | `db[Cls]`, raising `KeyError` if it has no table |
| list the tables | `SELECT name FROM sqlite_master` | `iter(d)` | left out |
| change a table | `ALTER TABLE`, or a rebuild | none | `migrate` and `rename` |
| insert | `INSERT` | `s.add(x)` | `db.add(record)`, which raises for a stored key, unlike `set.add` |
| upsert | `INSERT … ON CONFLICT DO UPDATE` | `d.update(…)` | `db.update(record)` |
| delete a record | `DELETE … WHERE` | `s.remove(x)`, `s.discard(x)` | `db.remove(record)`, `db.discard(record)` |
| delete by key | `DELETE … WHERE key = ?` | `del d[k]` | `db[Cls].delete(key)` |
| delete all | `DELETE FROM t` | `s.clear()` | `db[Cls].clear()` |
| select by key | `SELECT … WHERE key = ?` | `d.get(k)` | `db[Cls].get(key)` |
| exists, count, iterate | `SELECT 1`, `count(*)`, `SELECT` | `x in s`, `len(s)`, `iter(s)` | the same on `db[Cls]` |
| select | `SELECT … WHERE … ORDER BY … LIMIT` | comprehensions, `sorted`, slices | `filter`, `sorted`, slices |

`add` keeps raising for a stored key, because a silent no-op would hide mistakes; it is the one name whose behaviour differs from its built-in.

## The design

- `inlay.open(path=".inlay.db")` takes no classes.
  The history experiments still create their own tables there.
- `db.register(*classes)` creates the table and the indexes of each class, in one transaction, and returns nothing.
  - It raises `SchemaMismatch` if one of the tables exists, as `rename` does for a table that exists.
  - Each `Ref` target must be registered already, be the class itself, or be in the same call, or it raises and names the missing target.
    Classes that point to each other in a loop are registered in one call, so there is always an order that works.
  - It does not register the targets of `Ref`s by itself, so it never creates a table that was not named.
- `Cls in db` is true if the table of `Cls` exists, even if it differs from the class; it never raises.
- `db[Cls]`, and every write of a class, raise `KeyError` if the class has no table, as a dict does for a missing key and SQL does for a missing table.
  A filter through a `Ref` to a class with no table raises the same way.
  Writing does not create a table: on a plain dict, `d[k].add(x)` raises for a missing key, and only a `defaultdict` creates it.
- The db keeps a registry of the classes whose tables it knows are there and match.
  - A class that is not in it is looked up in the file, with the classes its `Ref`s reach, and added if their tables match; a table that differs raises `SchemaMismatch`.
    This also finds tables that another process registered.
  - `register` adds its classes, and `del db[Cls]` removes the class.
  - Any rollback, of a transaction or of a failed call's savepoint, empties the registry, since it may have undone a `register` or a `del`; the next use of each class looks it up again.
- Another process dropping or changing a table that this one already knows is not handled: the next statement fails with SQLite's own error, as in any database whose tables are dropped under it.
- `del db[Cls]` drops the table and its indexes, and raises `BrokenRef` while another table points to it.
  In the history db it drops the history of the class too.
- `migrate(fn, old, new, path=...)` loses `other_entities`; the db it passes to `fn` checks other classes on first use, as any db does.
- There is no list of the classes in a db.
  The file stores table names, not classes, and finding the class of a table would need a registry of every `@entity` class, which is global state: redefining a class, in a notebook or a test, gives two classes for one table, and what the list holds would depend on which modules are imported.
  Nothing needs it yet.
- Table names are not part of the API; the user works with classes.

### Startup

Programs that start several processes on the same file check and register inside a transaction, which takes the write lock first, so two processes cannot both find the table missing:

    with db.transaction():
        if Order not in db:
            db.register(Order)

Without the transaction it looks right and only fails when processes start together.

## What happens when a class is deleted from the code

Its table stays in the file, unread and unchecked, and no data is lost.
If another class pointed to it, that class's `Ref` has to change too, its table no longer matches, and its first use raises `SchemaMismatch` until a `migrate`.
A renamed class gets no table until it is registered; `rename` moves the old table to the new name.
`del db[Cls]` needs the class, so a table whose class is gone can only be dropped by defining a class for it again.

## Ideas set aside

- Setting a class up on its first write, and the classes its `Ref`s reach.
  It removes the explicit step, but a mistake creates a table silently, tables are created in the middle of normal traffic, and the same call behaves differently depending on earlier ones.
- `register` following `Ref`s and registering their targets: one call per class, but it creates tables that were not named.
- `setdefault` as the name: `dict.setdefault` takes one key and returns one value, and creating a missing table while keeping an existing one is the opposite of the strict `register` chosen here.
- `register` creating a missing table and checking an existing one: `Cls in db` before it does the same, so one strict operation is enough.
- `open` following `Ref`s from a shorter list: it keeps the list.
- A registry of every `@entity` class, used by `open` or for listing classes: global state, as above.
- Reads of a class with no table returning nothing: SQL raises for a missing table, and so does a dict for a missing key.

## Open

- The datoms version has no table for each class; it is paused, and gets the same API in the simplest form when it is picked up again.
- The error for `register` when a table exists: `SchemaMismatch` is proposed, as in `rename`.
