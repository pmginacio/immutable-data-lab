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

Every table gets a transaction column, and a column that says the version is a deletion.
The key of a row is the pair (record key, transaction), and a counter or a table of transactions numbers them.
A `Db` is a connection and a transaction number.

- Reading gives the latest version of each key up to that number, leaving out the keys whose latest version is a deletion.
- `add` and `update` insert a new version, and `remove` inserts a version that marks the record as deleted.
- Each write returns a new `Db`, and `db.as_of(tx)` goes back to an earlier one.

```python
db = db.add(ana, order)
db = db.update(replace(order, total=0))
db = db.remove(order)
before = db.as_of(first_tx)
```

The names are open.

Decisions and costs:

- A key now repeats across versions, so SQLite's primary and foreign keys cannot enforce uniqueness or references.
  `AlreadyExists` and `BrokenRef` become queries inside the write transaction.
- If another writer commits between two writes, the second write either fails, or its new `Db` includes the other writer's changes.
  The first version should fail.
- `db.add(x)` looks like a mutation, so a result that is not kept loses the write without a sound.
  pyright reports an unused call result with `reportUnusedCallResult`, which is off by default.
- Every read needs the latest version of each key, so it needs an index on (key, transaction).
  Nothing is ever deleted, and compaction is not designed.
- Grouping several writes in one transaction is not designed.
  `db.transaction()` as a block fits a mutable db, and an immutable one needs something else, such as one call that takes several verbs, or a builder.
- `migrate` and `rename` on a versioned db are not designed.

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
- Uniqueness and references are checked by queries, as in item 2.

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
