# Immutable database experiments

Notes from design discussions on 2026-11-13.
These are the history experiments of `2026-11-13-roadmap.md`.
The first version of item 2 is built, in `src/inlay/history/`, as designed below.

The background is `2026-11-10-immutable-data-and-databases.md`, which describes Datomic and XTDB.

## The goal

History: reading the data as it was at any past transaction.
That is what inlay does not have today, and it is the point of the experiment.

A db value that never changes under its reader comes with it, but it is not the goal on its own.
SQLite in WAL mode already gives a read transaction a fixed snapshot, which would be a much smaller experiment, but the old data only lasts as long as that transaction.

## Where it lives

In the same repo and package, as `src/inlay/history/`, with its own `db.py` and `sql.py`.
`entity`, `meta`, `nodes`, `conditions`, `functions` and `types` describe classes and conditions, know nothing about storage, and are imported from inlay.
A branch was not chosen, because the two versions should run the same read tests side by side.

## Item 2, first version: one table of versions per class

### Scope

The first version supports no change that touches history.

- Adding a new class is allowed: it is a new table, and the past does not change.
- Any change to an existing class is refused: `open` raises `SchemaMismatch`, as today.
- There is no `migrate`.
  `rename` does not change history, but it is left out to keep the first version small.

What schema changes could be allowed is a follow-up; see "Schema changes" below.

### Storage

Each class has one table that holds every version of every record.
A row has `tx_from` and `tx_to`: the transaction where the version starts, and the transaction where it stops being current.
A current version has `tx_to` equal to the largest 64-bit integer.

- `add` inserts a row.
- `update` sets `tx_to` of the current row to the transaction, and inserts the new version.
- `remove` sets `tx_to` of the current row.

The data columns of a row never change.
The only change to an existing row is `tx_to`, once, from current to a transaction number.

The primary key is SQLite's row id, not `(id, tx_from)`.
Two writes to the same record in one transaction then need no special case: the version made first in the transaction is closed in the same transaction, its range is empty, and no read ever sees it.
An ordinary index on `(id, tx_to)` serves the versions of one record.

### Integrity

SQLite enforces keys and references, as it does in inlay today, through two kinds of generated columns.

- `live_id` is the key on the current version and NULL on old ones.
  It is `UNIQUE`, so only one current version of a key exists, which gives `AlreadyExists`.
  Other tables point their references at it.
- `live_<ref>`, for each `Ref` field, is the reference on the current version and NULL on old ones.
  It has a deferred foreign key to the other class's `live_id`: a current record must refer to a current record, which gives `BrokenRef`.

NULLs do not count against `UNIQUE`, and SQLite does not check a foreign key with a NULL column, so old versions never block anything.
Updating a referenced record leaves the referring table alone: its key is missing for a moment inside the transaction, and the deferred check at commit finds the new version.

The `Order` of the README becomes:

```sql
CREATE TABLE "order" (
    "id" TEXT NOT NULL,
    "tx_from" INTEGER NOT NULL,
    "tx_to" INTEGER NOT NULL,
    "customer" TEXT NOT NULL,
    "total" INTEGER NOT NULL,
    "live_id" TEXT GENERATED ALWAYS AS (CASE WHEN "tx_to" = 9223372036854775807 THEN "id" END) VIRTUAL UNIQUE,
    "live_customer" TEXT GENERATED ALWAYS AS (CASE WHEN "tx_to" = 9223372036854775807 THEN "customer" END) VIRTUAL
        REFERENCES "customer" ("live_id") DEFERRABLE INITIALLY DEFERRED
) STRICT;
```

Checked in scratch scripts on SQLite 3.54:

- A unique generated column can be the parent of a foreign key, and a generated column can be the child.
- An order for a live customer is accepted, and one for a customer that never existed, or that was deleted, is refused.
- A second live customer with the same key is refused.
- Deleting a customer that a live order refers to is refused at commit, and deleting both in one transaction works.
- A deleted key can be added again, and its history continues.

Not possible in SQLite: an expression in a `REFERENCES` clause, a foreign key to a unique index on an expression, and an index or a foreign key on a view.
SQLite has no materialized views.

### Transactions

A table `inlay_tx (tx INTEGER PRIMARY KEY CHECK (tx < MAX), at TEXT NOT NULL)` numbers the transactions.
Each top-level transaction inserts a row right after `BEGIN IMMEDIATE`, and its writes use that number; a savepoint inside it shares it.
`at` is the UTC time from SQLite's own clock, stored as the larger of now and the previous `at`, so times never go backwards when the clock does.
SQLite's clock has milliseconds, so `as_of(time)` counts a transaction in the same millisecond as at or before it.
`as_of` compares times with `julianday`, which reads both SQLite's times and Python's, whose digits of a second differ.

The largest integer marks a current version, so a transaction must never get that number.
The `CHECK` on the table refuses it; the db then stays readable and refuses writes.
At a million transactions a second it would take about 290,000 years to get there.

### API

The db is the same object as in inlay, with the same `dict[type, Set]` interface.
It writes, and it reads the latest data; `db.transaction()` groups writes as today.

`db.as_of(tx)` takes a transaction number or a `datetime`, which means the last transaction at or before that time.
It returns a `Snapshot`: an immutable value pinned to one transaction, with the same read interface and no write methods.
`snapshot[Order]` is a `RecordView`, the read-only part that `RecordSet` already extends with writes.
Writing to the past is not possible, and pyright reports it, because a snapshot's type has no write methods.

```python
with inlay.history.open([Customer, Order]) as db:
    db.add(ana, order)
    with db.transaction():
        db.update(replace(order, total=0))
        db[Order].delete(old.id)
    big = db[Order].filter(Order.total > 100)

    last_week = db.as_of(datetime(2026, 11, 6))
    last_week[Order].filter(Order.total > 100)
```

A design in which a connection object writes and every db is an immutable value, as in Datomic, was considered.
It was not chosen because it breaks up the `dict[type, Set]` interface.

### Queries

The `Table` node gets the transaction it is read at: `Table(cls, tx)`, where no transaction means the latest data.
`sql.py` renders a table in one place, so every relation subquery and aggregate gets the same filter.

- Latest: `tx_to = 9223372036854775807`.
- At transaction T: `tx_from <= T AND T < tx_to`.

For example, `Order.customer.city == "Lisbon"` in a snapshot becomes `customer IN (SELECT id FROM customer WHERE tx_from <= ? AND ? < tx_to AND city = ?)`.

### Indexes

Most reads are of the latest data, which is an exact match on `tx_to`, so `tx_to` comes first.

- Each declared index becomes `(tx_to, …its fields)`.
  With `(tx_to, total)`, `tx_to = MAX AND total > 100` reads only current rows, in order.
  With `total` first, it would walk every version and discard the old ones.
- `(tx_to, tx_from)` serves reading a whole class, now or at T.
- `(live_<ref>)` for each `Ref`: SQLite needs an index that starts with the child column of a foreign key, or removing a record scans the referring table.
  Today the declared index of a `Ref` does both jobs; here the declared index serves reads and this one serves the foreign key.
- `(id, tx_to)` serves one record: its current version is an exact match on both columns, and a version at T is a range on `tx_to`.
  With `tx_from` second, as first built, a current version matched only on the key, and SQLite chose `(tx_to, tx_from)` instead, walking every current row to find one key.
  That made `update` of n records take time in proportion to n squared.

Reads at a past T have two ranges, and an index seeks on one, so they are served less well.
Whether this is good enough is for the benchmark to say.

### The iteration guard

It stays, with the same rule as today.
A write changes `tx_to` in rows, and in the indexes on `tx_to`, that an open query may be walking through, and SQLite does not say what an open query returns when that happens on the same connection.
A snapshot shares the db's connection, so the db and its snapshots share one count of open loops.

### Out of scope for the first version

- Erasing data for real, for example personal data. Datomic calls this excision.
- Compacting old versions; storage only grows.
- Detecting a lost update: reading at T and then updating a record that someone else changed since. The latest write wins, as in inlay.
- A snapshot with its own connection, for use in another thread.

## Why not append-only

Fully append-only storage, in which no row ever changes, was considered.
The versions of a key can still be kept in a line by keys alone: each row names the version it replaces, with `UNIQUE (id, prev)` and a foreign key from `(id, prev)` to `(id, tx)`.
That refuses adding a key that exists, two writes that replace the same version, and replacing a version that does not exist.

What keys cannot do is refuse a write because of rows elsewhere.
A constraint sees its own row, and whether a parent row exists.
In append-only storage rows never disappear, so a deleted customer's old row still satisfies a foreign key, and deleting it is an insert that nothing can refuse.
"A live order points to a live customer" then needs a query at commit or a trigger.

Known systems split the same way.

- A current table plus a history table: SQL:2011 system-versioned tables in SQL Server and MariaDB, and sqlite-history with triggers. The history table has no keys at all.
- One table of versions with a time range: type 2 slowly changing dimensions, Snodgrass, and temporal keys in PostgreSQL 18 (`WITHOUT OVERLAPS` and `PERIOD`). Closing a range changes the old row. This is the design chosen here, enforced for the latest data only, which is the only data that can be written.
- Append-only facts: Datomic and XTDB. They have no foreign keys, and references are checked by the application or in transaction functions.

## Schema changes: a follow-up

The first version allows none.
The follow-up designs which changes to allow and how.
Three problems came up.

1. A change to a shape rewrites history.
   Converting old versions shows fields that did not exist, with values computed today.
   Keeping old versions in the old shape needs a table per shape, a record of which table covers which transactions, and a way to read them.
2. A change spreads through references.
   A class that others refer to gets a new table, so the referring classes change shape too, and so on.
3. Old code is gone.
   Rows written in an old shape cannot become objects of a class that no longer exists in the code.

Known answers:

- Only grow, and give a breaking change a new name (Rich Hickey's "Spec-ulation"; Datomic's schema; Avro and Protobuf, where adding or removing an optional field is compatible and changing a type is not).
  With optional fields, adding a field is done in place: old versions have no value for it, and nothing spreads.
- Store the schema in the db, with its own history (Avro's writer schema, Datomic's schema as data).
  A catalog of each class's fields per range of transactions makes old rows readable after the code moved on.
- Convert old rows to the current class when they are read (upcasting, from event sourcing), with one small function per breaking change.
  A filter on a new field cannot run in SQL on old rows, and would need the planned Python evaluator.
- Generate a class from the stored schema (`dataclasses.make_dataclass`) when there is no upcaster.
- Point references at an identity that outlives a shape, not at the table of one class, as Datomic does.
  It is the only known fix for problem 2.
- Rewrite queries across schema versions (PRISM and PRISM++, Curino et al.). Complete, and far too much machinery here.

A migration in an immutable db may simply be an ordinary transaction: add the records of a new class, built from the old ones, and remove the old ones, whose history stays.

## Item 3: datoms

The db stores facts, one for each field of a record: the entity, the attribute, the value, the transaction, and whether it was added or retracted.
A record is the facts of one entity.
A change retracts the old fact and adds a new one, and a deletion retracts all the facts of the entity.
A relation is a fact whose value is the key of another entity.

What it could give:

- An optional field is a fact that is not there, so there is no NULL.
- Adding a field needs no table rebuild, which answers part of the schema change problems.
- History is kept for each field, and a change stores only the field that changed.

What it costs:

- Reading a record gathers its facts, so a read touches more rows than in a table of versions.
- One value column cannot hold values of different types under `STRICT`.
  It needs a table for each type, or an untyped column.
- A condition on a field is a lookup of facts, and a condition through a relation is a join over facts.
- The live columns of item 2 should fit: a fact gets `tx_from` and `tx_to`, and a fact whose value is a reference goes in a table of its own, so that it can have a foreign key to the entity.
  This was not tried.

Item 3 reuses the transaction numbers, the snapshots and the live columns of item 2, so item 2 comes first.

## How to compare them

The versions should run the same reading code.
The read tests of inlay (`test_read.py`, `test_condition_sql.py`, and the like) can run against each, and the write tests carry over as long as the write API stays the same.

What to measure:

- lines of code in the storage part,
- how hard it is to compile a query,
- speed of bulk insert, load, a filter through a relation, many `.get()` calls, and reads at a past transaction,
- how much storage a record that is updated many times takes,
- how well the API fits Python.

Sources for the known systems: the PostgreSQL 18 temporal constraints, the SQL Server temporal table limitations, the XTDB key concepts, the Datomic schema reference, sqlite-history, Confluent's schema evolution rules, writing on event upcasting, and the PRISM papers.
