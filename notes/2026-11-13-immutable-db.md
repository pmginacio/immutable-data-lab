# Immutable database experiments

Notes from a design discussion on 2026-11-13.
Nothing here is decided or built yet.
These are items 2 and 3 of `2026-11-13-roadmap.md`.

The background is `2026-11-10-immutable-data-and-databases.md`, which describes Datomic and XTDB.

## The idea

The db object is pinned to one transaction, and it never changes.
A write returns a new db object that points to the new transaction, and the old one still sees the data as it was.
Data is only added: to delete a record, a marker is added that says it is deleted.

What it gives:

- A read never changes under the reader, and an old db object is a point in time that can be read again.
- The iteration guard is no longer needed, because a write cannot change what an open read sees.
  The guard is about 42 source lines and 108 test lines.
- A db object is a value that can be passed to another thread.
  The connection under it still has to be handled.

## Where it would live

Most of inlay would be shared: `entity`, `meta`, `nodes`, `conditions`, `functions` and `types` describe classes and conditions, and they know nothing about how records are stored.
The storage is where the versions differ, and it sits in two places.

- Reads: `sql.py` renders the table of a query in one place (`_Select(m, quote(m.table))`).
  In a versioned db that place becomes a subselect of the latest version of each key as of the pinned transaction.
- Writes and the schema: the create, insert and delete statements in `sql.py`, and `Db`, `open` and the transactions in `db.py`.

So the experiment copies `db.py` and the storage part of `sql.py` into a new module, and imports the rest from `inlay`.
It does not copy the whole package.
Whether it lives in its own directory or on its own branch is not decided.

## Item 2: versioned rows

Each class has one table of versions.
A row has `tx_from` and `tx_to`: the transaction where the version starts, and the transaction where it stops being current, which is the largest integer while the version is current.
A counter or a table of transactions numbers them.
A `Db` is a connection and a transaction number.

- Reading at transaction T gives the rows with `tx_from <= T < tx_to`. It is a plain range, and an index can serve it.
- `add` inserts a row.
- `update` sets `tx_to` of the current row and inserts the new version.
- `remove` sets `tx_to` of the current row, which marks the record as deleted.
- Each write returns a new `Db`, and `db.as_of(tx)` goes back to an earlier one.

Only `tx_to` of an existing row ever changes, and the data columns never do.

```python
db = db.add(ana, order)
db = db.update(replace(order, total=0))
db = db.remove(order)
before = db.as_of(first_tx)
```

The names are open.

Decisions and costs:

- Closing a row changes its `tx_to` in place, and an update is two statements, a close and an insert.
- Uniqueness and references are enforced by SQLite's own keys, which needs the interval keys of the next section.
- If another writer commits between two writes, the second write either fails, or its new `Db` includes the other writer's changes.
  The first version should fail.
- `db.add(x)` looks like a mutation, so a result that is not kept loses the write without a sound.
  pyright reports an unused call result with `reportUnusedCallResult`, which is off by default.
- A read is a range on `tx_from` and `tx_to`, and the declared indexes of a class are not designed for it yet.
  Nothing is ever deleted, and compaction is not designed.
- Grouping several writes in one transaction is not designed.
  `db.transaction()` as a block fits a mutable db, and an immutable one needs something else, such as one call that takes several verbs, or a builder.
- `migrate` and `rename` on a versioned db are not designed.

### Integrity with SQLite's foreign keys

The goal is that SQLite enforces all of this, as it does in inlay today:

- a key is created once, which gives `AlreadyExists`,
- a reference points to a record that exists now, so a deleted record is not a valid target,
- a record that a live record refers to cannot be deleted, which gives `BrokenRef`,
- the order of writes inside a call does not matter, because the checks are deferred.

A foreign key needs its parent key to be unique, and a key repeats across versions.
In SQLite 3.54, a foreign key to a column that repeats, and a foreign key to a partial unique index, both fail with `foreign key mismatch`.
A foreign key to `(id, tx)` is valid, but it points to one version of the record, not to the record.

The design that works makes the compound key `(id, tx_to)` unique and uses it as the parent key.
It exists for a record exactly until the record is deleted, because `tx_to` is the largest integer only while the version is current.
The child side is a virtual column that is that same value only while the child row is current, and NULL otherwise.
SQLite does not check a foreign key with a NULL column, so old and deleted versions of the child never block anything, and no extra update is needed.

```sql
CREATE TABLE customer (id TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY (id, tx_from), UNIQUE (id, tx_to)) STRICT;
CREATE TABLE "order" (id TEXT NOT NULL, tx_from INTEGER NOT NULL, tx_to INTEGER NOT NULL, customer TEXT NOT NULL,
    customer_to INTEGER GENERATED ALWAYS AS (CASE WHEN tx_to = 9223372036854775807 THEN tx_to END) VIRTUAL,
    PRIMARY KEY (id, tx_from), UNIQUE (id, tx_to),
    FOREIGN KEY (customer, customer_to) REFERENCES customer (id, tx_to) DEFERRABLE INITIALLY DEFERRED) STRICT;
```

Tested in a scratch script, each result coming from SQLite:

- An order for a customer that never existed is refused.
- A second live customer with the same key is refused on the unique key.
- Deleting a customer that a live order refers to is refused at commit.
- Deleting the customer and then the order in one call works.
- An order for the deleted customer is refused.
- Adding the deleted key again works: the record is revived, and its history continues.
- Updating a customer while an order refers to it works, although the live key is missing for a moment in the middle of the call.
- Reading at transaction T with `tx_from <= T < tx_to` gives the right customer at each point.
- The virtual column works as part of a foreign key, also in a `STRICT` table.

Only SQLite 3.54 was tested.
The scratch script is not kept, and the SQL above is what it ran.

Two other designs were tried and are not needed any more.

- An identity table with one row for each key ever created, and a table of versions.
  The foreign keys point to the identity table.
  It gives `AlreadyExists`, and refuses a reference to a key that never existed, but a deleted record stays a valid target, so the other checks need queries.
- A live table for each class, with only the key and the `Ref` columns of the records that are not deleted, and foreign keys between live tables.
  It enforces everything, and it keeps the version rows unchanged, but it is a second table that is changed in place.
  Triggers on the version table could keep it up to date, but `add`, `update` and `remove` all append a version, so they would need a column that says which one it is.

A `current` flag with a unique index was rejected: it changes old rows on every write, and a foreign key cannot use a partial index.

## Item 3: datoms

The db stores facts, one for each field of a record: the entity, the attribute, the value, the transaction, and whether it was added or retracted.
A record is the facts of one entity.
A change retracts the old fact and adds a new one, and a deletion retracts all the facts of the entity.
A relation is a fact whose value is the key of another entity.

What it could give:

- An optional field is a fact that is not there, so there is no NULL.
- Adding a field to a class needs no table rebuild.
- History is kept for each field, and a change stores only the field that changed.

What it costs:

- Reading a record gathers its facts, so a read touches more rows than in a table of versions.
- One value column cannot hold values of different types under `STRICT`.
  It needs a table for each type, or an untyped column.
- A condition on a field is a lookup of facts, and a condition through a relation is a join over facts.
- The interval keys of item 2 should fit: a fact gets `tx_from` and `tx_to`, and a fact whose value is a reference goes in a table of its own, so that it can have a foreign key to the entity.
  This was not tried.

The difference between the two versions is the unit that is versioned: a whole row in item 2, and a single fact in item 3.
Item 3 reuses the transaction number, the pinned `Db` and the deletion markers of item 2, so item 2 comes first.

## How to compare them

The three should run the same reading code.
The read tests of inlay (`test_read.py`, `test_condition_sql.py`, and the like) use `inlay.open`, and the write tests do not carry over, because a write returns a new db.
The write tests would be rewritten for each experiment.

What to measure:

- lines of code in the storage part,
- how hard it is to compile a query,
- speed of bulk insert, load, a filter through a relation, and many `.get()` calls,
- how much storage a record that is updated many times takes,
- how well the API fits Python.

## Open questions

- Own directory or own branch for the experiment?
- Fail or include the other writer's changes, when a write meets a newer transaction?
- How are several writes grouped in one transaction?
- The names of the verbs, and of `as_of`.
- What `migrate` and `rename` mean for a versioned db.
- How transactions are numbered: a table of transactions, or one more than the largest number in use.
- Which indexes a class needs to serve the range on `tx_from` and `tx_to`.

## Where we left off

The design of item 2 is settled up to the open questions above, and no code has been written.
The next step is to decide where the experiment lives, and to settle the other questions.
After that, copy `db.py` and the storage part of `sql.py` into a new module, and change them to the interval tables.
Item 1 of the roadmap, the default db file, is independent and still to do.
