# Immutable database experiments

Notes from design discussions on 2026-11-13.
These are the history experiments of `2026-11-13-roadmap.md`.
The first version of the history db is built, as `inlay.history` in `src/inlay/history/`, and the sections below describe it as built.

The background is `2026-11-10-immutable-data-and-databases.md`, which describes Datomic and XTDB.

## The goal

History: reading the data as it was at any past transaction.
That is what inlay does not have today, and it is the point of the experiment.

A db value that never changes under its reader comes with it, but it is not the goal on its own.
SQLite in WAL mode already gives a read transaction a fixed snapshot, which would be a much smaller experiment, but the old data only lasts as long as that transaction.

## Where it lives

In the same repo and package, as `src/inlay/history/`, with its own `db.py` and `sql.py`.
The datoms version starts as a copy of it in `src/inlay/datoms/`.
`entity`, `meta`, `nodes`, `conditions`, `functions` and `types` describe classes and conditions, know nothing about storage, and are imported from inlay.
A branch was not chosen, because the two versions should run the same read tests side by side.

## The history db, first version

It keeps one table of versions per class.

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
- `update` sets `tx_to` of the current row with each record's key, if there is one, and then inserts the records as new versions.
  So the same key twice in one call inserts two current versions, and the second raises `AlreadyExists`; inlay's upsert keeps the last record instead.
- `remove`, `discard`, `delete` and `clear` set `tx_to` of the current rows they match.

The data columns of a row never change.
The only change to an existing row is `tx_to`, once, from current to a transaction number.

The primary key is SQLite's row id, not `(id, tx_from)`.
Two writes to the same record in one transaction then need no special case: the version made first in the transaction is closed in the same transaction, its range is empty, and no read ever sees it.
An ordinary index on `(id, tx_to)` serves the versions of one record.

In the stored tables the columns are `_tx_from`, `_tx_to` and `_live_…`, and the table of transactions is `_inlay_tx`.
Every name the history db adds starts with an underscore, so it cannot clash with a field, a class or a declared index, whose names never start with one in practice.
This note writes them without the underscore, except where it shows the SQL.

### Integrity

SQLite enforces keys and references, as it does in inlay today, through two kinds of generated columns.

- `live_id` is the key on the current version and NULL on old ones.
  It is `UNIQUE`, so only one current version of a key exists, which gives `AlreadyExists`.
  Other tables point their references at it.
- `live_<ref>`, for each `Ref` field, is the reference on the current version and NULL on old ones.
  It has a deferred foreign key to the other class's `live_id`: a current record must refer to a current record, which gives `BrokenRef`.

NULLs do not count against `UNIQUE`, and SQLite does not check a foreign key with a NULL column, so old versions never block anything.
Updating a referenced record leaves the referring table alone: its key is missing for a moment inside the transaction, and the deferred check at commit finds the new version.

The `Order` of the README becomes, with the fields first, then the version columns, then the `live_` columns:

```sql
CREATE TABLE "order" (
    "id" TEXT NOT NULL,
    "customer" TEXT NOT NULL,
    "total" INTEGER NOT NULL,
    "_tx_from" INTEGER NOT NULL,
    "_tx_to" INTEGER NOT NULL,
    "_live_id" TEXT GENERATED ALWAYS AS (CASE WHEN "_tx_to" = 9223372036854775807 THEN "id" END) VIRTUAL UNIQUE,
    "_live_customer" TEXT GENERATED ALWAYS AS (CASE WHEN "_tx_to" = 9223372036854775807 THEN "customer" END) VIRTUAL
        REFERENCES "customer" ("_live_id") DEFERRABLE INITIALLY DEFERRED
) STRICT;
```

The stored text is one line; `test_history_sql.py` has it exactly.

Checked in scratch scripts on SQLite 3.54:

- A unique generated column can be the parent of a foreign key, and a generated column can be the child.
- An order for a live customer is accepted, and one for a customer that never existed, or that was deleted, is refused.
- A second live customer with the same key is refused.
- Deleting a customer that a live order refers to is refused at commit, and deleting both in one transaction works.
- A deleted key can be added again, and its history continues.

Not possible in SQLite: an expression in a `REFERENCES` clause, a foreign key to a unique index on an expression, and an index or a foreign key on a view.
SQLite has no materialized views.

### Transactions

A table `_inlay_tx (tx INTEGER PRIMARY KEY, at INTEGER NOT NULL)` numbers the transactions.
A top-level transaction's writes use the next number, one more than the largest recorded: it holds the write lock from `BEGIN IMMEDIATE`, so no other connection can take that number first.
It records its row, with the number and the time, as the last step before `COMMIT`; a savepoint inside it is part of it.
So the time is when the transaction commits, `db.tx` and `as_of` only ever see committed transactions, and a rollback leaves no row behind.
A transaction that changes nothing, such as a read-only `db.transaction()` block or a `clear` that matches no record, still takes a number.
That is harmless: no version starts or ends at it, so a snapshot at it shows the same data as one at the transaction before.
`at` is the time from SQLite's own clock, `unixepoch('now', 'subsec')`, in whole milliseconds since 1970 in UTC.
It is stored as the larger of now and the previous `at`, so times never go backwards when the clock does.
`as_of(time)` turns its time into the same milliseconds and compares numbers, so a transaction in the same millisecond counts as at or before it.
In a SQLite shell, `datetime("at" / 1000, 'unixepoch')` shows a time as a date.

The largest integer marks a current version, so the transaction that got that number would leave versions that look current forever.
Nothing refuses it: at a million transactions a second it would take about 290,000 years to get there.

### API

The db has the same interface as inlay's, the same `dict[type, Set]`.
It writes, and it reads the latest data; `db.transaction()` groups writes as today.
Its default file is `.inlay-history.db`, so it never opens inlay's `.inlay.db` by accident.

`db.as_of(tx)` takes a transaction number or a `datetime`, which means the last transaction at or before that time.
It returns a `Snapshot`: an immutable value pinned to one transaction, with the same read interface and no write methods.
`snapshot[Order]` is a `RecordView`, the read-only part that `RecordSet` already extends with writes.
Writing to the past is not possible, and pyright reports it, because a snapshot's type has no write methods.

```python
with inlay.history.open([Customer, Order]) as db:
    db.add(ana, order, other_order)
    with db.transaction():
        db.update(replace(order, total=0))
        db[Order].delete(other_order.id)
    big = db[Order].filter(Order.total > 100)

    last_week = db.as_of(datetime(2026, 11, 6, tzinfo=UTC))
    last_week[Order].filter(Order.total > 100)
```

A design in which a connection object writes and every db is an immutable value, as in Datomic, was considered.
It was not chosen because it breaks up the `dict[type, Set]` interface.

### Queries

A view carries the transaction it reads at, and None for the latest data.
It passes it to `select_sql`, `aggregate_sql` and `condition_sql`, and `sql.py` renders every table through one place, so relation subqueries and aggregates get the same filter.
The query tree does not change: conditions are built without a db, so a `Table` inside a relation cannot know the transaction.

- Latest: `(SELECT … FROM t WHERE tx_to = 9223372036854775807)`.
- At transaction T: `(SELECT … FROM t WHERE tx_from <= ? AND ? < tx_to)`, with T passed twice.

For example, `Order.customer.city == "Lisbon"` in a snapshot becomes `customer IN (SELECT id FROM (SELECT … FROM customer WHERE tx_from <= ? AND ? < tx_to) WHERE city = ?)`.

### Indexes

Most reads are of the latest data, which is an exact match on `tx_to`, so `tx_to` comes first.

- Each declared index becomes `(tx_to, …its fields)`.
  With `(tx_to, total)`, `tx_to = MAX AND total > 100` reads only current rows, in order.
  With `total` first, it would walk every version and discard the old ones.
- `(tx_to, tx_from)` serves reading a whole class, now or at T.
- `(live_<ref>)` for each `Ref`: SQLite needs an index that starts with the child column of a foreign key, or removing a record scans the referring table.
  It is a partial index, `WHERE live_<ref> IS NOT NULL`, so old versions add nothing to it; the foreign key check only looks for real values.
  In inlay the declared index of a `Ref` does both jobs; in the history db the declared index serves reads and this one serves the foreign key.
- `(id, tx_to)` serves one record: its current version is an exact match on both columns, and a version at T is a range on `tx_to`.
  With `(id, tx_from)`, a current version would match only on the key, and SQLite would choose `(tx_to, tx_from)` instead, walking every current row to find one key.
  That makes `update` of n records take time in proportion to n squared.

Reads at a past T have two ranges, and an index seeks on one, so they are served less well.
The benchmark below measures how much.

### Benchmark

`benchmarks/history.py` runs the same writes and reads through inlay and the history db.
The numbers here are for 20,000 orders of 2,000 customers, with every order updated 10 times, on Python 3.15 and SQLite 3.54.

Reads of the latest data cost about the same as in inlay.
Loading, filters and `get` are within 1.0 to 1.2 times inlay's time; a filter through a `Ref` and `len` with `sum` take 1.6 and 2 times, under a millisecond either way.

Writes cost more.

| write | inlay | history |
|---|---|---|
| add everything | 79 ms | 147 ms |
| update all orders, one round | 104 ms | 1,115 ms |
| 1,000 updates of one order, one call each | 71 ms | 264 ms |
| remove 1,000 orders | 12 ms | 129 ms |
| update all 2,000 customers | 4 ms | 20 ms |
| 200 customers and 2,000 orders in one call | 27 ms | 242 ms |

Almost all of it is in SQLite: Python takes about 80 ms of a 1.3 s update round, the same as in inlay.
Ending the old versions takes about 1 s of it, because changing `tx_to` moves the row's entry in every index on it, at 150 to 330 ms per index.
Inserting the new versions takes about 120 ms.
Foreign key checks cost nothing measurable, and neither does the subquery for the transaction number in every row.
Replacing a customer that orders refer to costs no more than replacing an order.

Reads at a past transaction cost more the older the transaction, because SQLite walks every version that ended after it.
At the first transaction, loading all orders takes 53 ms against 39 now, and a filter takes 16 to 19 ms against under half a millisecond.
`as_of(datetime)` takes 0.03 ms at this size.

The file is 62 MB against 4 MB, for 11 versions of every order.

#### Index order

The benchmark also runs the history db with its declared indexes the other way round, `(field, tx_to)`.

| read | `(tx_to, field)` | `(field, tx_to)` |
|---|---|---|
| equality filter at the first transaction | 18 ms | 0.11 ms |
| filter through a `Ref` at the first transaction | 16 ms | 0.5 ms |
| range filter at the first transaction | 19 ms | 4.1 ms |
| range filter now | 0.44 ms | 3.8 ms |
| sorted, first 10, now | 0.07 ms | 4.5 ms |
| one more update round | 1,286 ms | 1,423 ms |

Field first serves past filters far better, and current range filters and sorting far worse, and it does not make writes faster.
Most reads are of the latest data, so `tx_to` stays first.
Fast past filters would need the field-first indexes in addition, at the cost of slower writes.

### The iteration guard

It stays, with the same rule as today.
A write changes `tx_to` in rows, and in the indexes on `tx_to`, that an open query may be walking through, and SQLite does not say what an open query returns when that happens on the same connection.
A snapshot shares the db's connection, so the db and its snapshots share one count of open loops.

### Out of scope for the first version

- Erasing data for real, for example personal data.
  Datomic calls this excision.
- Compacting old versions; storage only grows.
- Detecting a lost update: reading at T and then updating a record that someone else changed since.
  The latest write wins, as in inlay.
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

- A current table plus a history table: SQL:2011 system-versioned tables in SQL Server and MariaDB, and sqlite-history with triggers.
  The history table has no keys at all.
- One table of versions with a time range: type 2 slowly changing dimensions, Snodgrass, and temporal keys in PostgreSQL 18 (`WITHOUT OVERLAPS` and `PERIOD`).
  Closing a range changes the old row.
  This is the design chosen here, enforced for the latest data only, which is the only data that can be written.
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
- Rewrite queries across schema versions (PRISM and PRISM++, Curino et al.).
  It is complete, and far too much machinery here.

A migration in an immutable db may simply be an ordinary transaction: add the records of a new class, built from the old ones, and remove the old ones, whose history stays.

## The datoms version

The db stores datoms, one for each field of a record: the entity, the attribute, the value, the transaction, and whether it was added or retracted.
A record is the datoms of one entity.
A change adds a datom with the new value, and a deletion retracts all the datoms of the entity.
A relation is a datom whose value is the key of another entity.

What it could give:

- An optional field is a datom that is not there, so there is no NULL.
- Adding a field needs no table rebuild, which answers part of the schema change problems.
- History is kept for each field, and a change stores only the field that changed.

What it costs:

- Reading a record gathers its datoms, so a read touches more rows than in a table of versions.
- One value column cannot hold values of different types under `STRICT`, except as `ANY`.
- A condition on a field is a lookup of datoms, and a condition through a relation is a join over datoms.

### Design

It lives in `src/inlay/datoms/`, which started as a copy of the history db.
It keeps the history db's transaction numbers, `as_of` and snapshots, and the same API.

Storage is one table for every class, `_inlay_datoms`, written only by inserts:

    _inlay_datoms(class, e, a, v, tx, added)

- `class` is the class's table name, and `e` is the record's key, so keys stay unique within a class, as in inlay.
  The class has its own column instead of a namespace in `a`, as Datomic has, because `e` alone is not unique across classes.
- `a` is the field name; a record has one datom for each field, the key too.
  The key's datom repeats `e`, but it is what says whether a record is stored, even for a class with no other field.
- `v` is `ANY`: `to_sql` writes it and `from_sql` reads it with the field's type, and a comparison only ever meets the values of one field.
- `added` is false for a retraction, as in Datomic, which keeps the value it retracts.
  The value of a field at T is its newest datom at or before T, if that datom is added.
  A change adds the new value; a removal retracts every field.
- A second write to a field in the same transaction replaces the first, as an empty version does in the history db.
- An update adds a datom only for the fields that changed, compared in SQL, so an identical update adds none, though it still takes a transaction number.
  The same key twice in one `update` keeps the last record, as in inlay.

Indexes, in the terms of Datomic's:

- EAVT is the table itself: `WITHOUT ROWID`, with primary key `(class, e, a, tx)`.
  It serves reading a record, a get by key, the newest datom of a field, and reading a whole class.
- AVET will be a partial index `(class, a, v, e, tx) WHERE class = … AND a = …` for each field of a declared index, as Datomic only indexes the attributes marked for it.
  Step A creates none, because its filters cannot use them.
- VAET is the same partial index on each `Ref` field, which finds the records that point to a record.
- The log is an index on `tx`, which finds the datoms of the open transaction for the reference checks.
  Passing along the records the writes touched would avoid it, at the cost of state kept in Python across calls and savepoints.
- AEVT is left out.

There are no foreign keys or unique constraints: the library checks them.
`AlreadyExists` and `Missing` are checked as each call runs, by the row count of an insert that only writes when the record is stored, or not.
`BrokenRef` is checked before a top-level transaction commits, so the order of writes does not matter, with two SELECTs for each `Ref` field: a reference added in the transaction to a record that is not stored, and a record removed in the transaction that a stored reference still points to.

The iteration guard stays per class: datoms of another class never match a loop's `class = …`, so writing them cannot change what the loop returns.

`open` does not check the classes against the stored datoms in the first version.

### Queries

A class at T becomes a subquery that rebuilds its rows from the newest datom of each field, with one row per record from `max(CASE a WHEN … THEN v END)` for each field, where the key's datom is added.
The rest of the compiler stays as it is, on top of that subquery.

It groups only once, by `e`, and picks the newest datoms with `NOT EXISTS` a newer one, which is a search on the primary key for each datom.
The form with two groupings, the newest datom of each field by `max(tx)` and then one row per record, reads the datoms in one pass, but SQLite does not push a condition on the key through two groupings, so a `get` rebuilt the whole class.

The risk is filters: a condition on the rebuilt rows cannot reach the partial indexes, so every filter rebuilds the whole class.
A condition on the key is pushed down, since `e` is the `GROUP BY` column.

The steps:

1. A: the rebuilt rows only, then the benchmark.
2. B: a condition on a field also compiles to a lookup of datoms, `e IN (SELECT e FROM _inlay_datoms WHERE class = ? AND a = ? AND v > ? AND …newest at T…)`, through the partial index, if the benchmark shows filters are slow.

Compiling every query to joins over datoms, as Datalog does, was left out.
Each field read costs an index search, and another to check it is the newest at T, so loading many records costs several times as much as one pass, and it needs a new compiler for conditions, sorting, slices and aggregates.

## How to compare them

The versions should run the same reading code.
The tests that go through the db's API (`test_read.py`, `test_write.py`, `test_iteration.py` and parts of `test_db.py`) run against all of them.
`test_as_of.py` runs the reads at past transactions against both history experiments, and `test_history.py` and `test_datoms.py` check how each stores its data.
Tests of the SQL text, such as `test_condition_sql.py`, check inlay's own `sql.py` only.

What to measure:

- lines of code in the storage part,
- how hard it is to compile a query,
- speed of bulk insert, load, a filter through a relation, many `.get()` calls, and reads at a past transaction,
- how much storage a record that is updated many times takes,
- how well the API fits Python.

Sources for the known systems: the PostgreSQL 18 temporal constraints, the SQL Server temporal table limitations, the XTDB key concepts, the Datomic schema reference, sqlite-history, Confluent's schema evolution rules, writing on event upcasting, and the PRISM papers.
