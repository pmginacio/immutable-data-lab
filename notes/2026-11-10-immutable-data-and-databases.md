# Immutable data, FP style, and databases in Python

Notes from a learning session on 2026-11-10.
No code was written.
Library names and Datomic details are from memory, so check them before relying on them.

## Where we ended up

- For everyday FP-style Python, frozen dataclasses and tuples are enough.
  Persistent data structures are for large collections where old versions stay alive.
- The cleanest way to reduce SQL in Python code is flat, immutable classes where each class declares its table, primary key, indexes and references.
  A small generic module then does load, upsert, delete, filters and related-table filters.
- Queries can be immutable values that compile to SQL and return typed, flat result classes.
  SQLAlchemy Core covers the building and compiling.
  The thin layer on top maps rows into frozen classes.
- A Datomic-style fact model on SQLite is a good learning project.
  It is not the first thing to build if the goal is just less SQL.

## Immutable data and structural sharing

An update copies only the path from the root to the changed node and reuses the rest.
That makes an update O(log n) instead of O(n), and old versions stay valid.

Useful for undo, snapshots, safe sharing between threads, and cheap change detection.
Costs more memory per element and slower reads.
Sharing only pays off if old versions are kept.

Python options:

- The standard library has `tuple`, `frozenset` and `MappingProxyType`.
  They are immutable but copy everything on update.
- `pyrsistent`: the most complete, mostly pure Python.
- `immutables`: a C hash trie `Map`, used by `contextvars`.
- `rpds-py`: Rust bindings with a hash trie map, set, list and queue.
- `frozendict`: copies on update.

Batch updates with `evolver()` or `Map.mutate()`.
`rpds.List` is a linked list, so indexing is O(n).
Small collections are faster with plain copying.

## FP style in everyday Python

- `@dataclass(frozen=True, slots=True)` for records, `dataclasses.replace` to update.
- `tuple` and `frozenset` instead of `list` and `set`.
- `Sequence` and `Mapping` in type hints.
- Pure functions in the middle, side effects at the edges.
- Frozen is shallow: a `list` field is still mutable.

## The impedance mismatch

Two separate problems:

1. Object graphs versus flat rows with keys.
2. SQL embedded in strings.

The second one is a tooling problem with tooling fixes.
SQL itself is good at joins, filters and aggregates.

Ways to deal with it: a full ORM, a query builder, SQL in `.sql` files, or a different data model.
Different models: documents, object databases, graphs, fact-based databases like Datomic.
Or change the programming side: plain data (Clojure) or queries in the language (LINQ).

## Plain data and flat classes

Nested frozen dataclasses work, but updating a deep field means rebuilding every level.

Flatten in two ways:

- Inline a nested record that only groups fields.
- Normalize an entity with its own identity: store it in its own table and refer to it by id.

Invariants go in `__post_init__`.
Because the object is frozen, the check holds for its whole life.
It is shallow, and it cannot cover rules that span several objects.
Those go in the function that builds the new state, or in the database.

Pydantic helps with converting rows to classes and with coercion.
`model_copy(update=...)` does not run validation, so rebuild with `model_validate` instead.

## Getting SQL out of the Python code

Each class declares what the database layer needs:

```python
@dataclass(frozen=True, slots=True)
class Order:
    table: ClassVar[str] = "orders"
    pk: ClassVar[str] = "id"
    refs: ClassVar[dict[str, type]] = {"customer_id": Customer}
    indexes: ClassVar[tuple[tuple[str, ...], ...]] = (("customer_id",),)
    id: int
    customer_id: int
    total: int
```

From that, generic code can:

- Load and upsert (`INSERT ... ON CONFLICT DO UPDATE`).
- Generate `CREATE TABLE ... STRICT` and `CREATE INDEX`.
- Check that filters use an index (leftmost-prefix rule for composite indexes).
- Filter on keyword equality, or on expression objects (`c.city == "Lisbon"`).
- Filter by a related table with a subquery: `customer_id IN (SELECT id FROM customers WHERE ...)`.

Limits: database-generated ids need `replace(obj, id=...)`, partial column loads need their own class, and migrations cannot be derived.
Two references to the same table need an explicit column name.
Joins that return columns from several tables need a real join and a dedicated result class.

SQLite has no partitioning.
Foreign keys are off unless `PRAGMA foreign_keys = ON` is set on each connection.
Use `STRICT` tables for real type checks.

## Relations

- One-to-many: a ref on the "many" side.
- One-to-one: a ref plus a uniqueness check.
- Many-to-many: a join table, or a cardinality-many ref in the fact model.
- Foreign keys: let SQLite enforce them, or check on write in the fact model.

## How other languages do it

- Elixir Ecto: schemas are structs, explicit `Repo`, changesets for validation, composable queries.
  It is the closest to this design.
- Clojure: rows as plain maps, HoneySQL (SQL as data), HugSQL (SQL in files).
- Haskell: typed query libraries.
- Scala: Doobie (queries as values), Quill.
- F#: type providers.
- Rust: `sqlx` (compile-time checked SQL), Diesel.
- C#: LINQ.

## Storage without a traditional database

A database also gives durability, atomic transactions and safe concurrency.
Those are the hard parts.

Options: SQLite, LMDB, Parquet with DuckDB or Polars, an append-only log with an in-memory index.

Append-only log:

- Each write appends a checksummed record, then `flush` and `fsync`.
  On macOS, `F_FULLFSYNC` may be needed for a real flush.
- On startup, replay the log and truncate the first bad record.
- State is a fold over the log.
- Hash index for keys, secondary dict indexes for filters, sorted structures for ranges.
- Later: segments with summaries, snapshots, compaction, and the LSM tree design.
- LMDB's copy-on-write B-tree is structural sharing on disk.

SQLite:

- One file, B-tree pages, one writer at a time.
- Rollback journal by default, WAL mode with `PRAGMA journal_mode=WAL`.
- It fits a single-writer app that needs durability and atomic transactions.

## Datomic

- The database is a growing set of datoms: entity, attribute, value, transaction, added flag.
- Schema is a list of attributes with a type and cardinality.
  Refs point to other entities.
- A change retracts the old fact and asserts the new one.
  A retraction repeats the fact with `added = false`.
- Queries are Datalog patterns.
  A shared variable is the join.
- `db` is an immutable value, so `as-of` and `history` come for free.
- Four indexes: EAVT, AEVT, AVET, VAET.
- One transactor writes.
  Readers cache immutable segments.
- Arrays: cardinality many (an unordered set), or entities with a position, or an opaque value.
- Downsides: single writer limit, history grows, JVM-centred, small ecosystem, weak Python support.

## A Datomic-like layer on SQLite

```sql
CREATE TABLE datoms (
  e INTEGER NOT NULL, a INTEGER NOT NULL, v NOT NULL,
  tx INTEGER NOT NULL, added INTEGER NOT NULL
);
CREATE INDEX eavt ON datoms (e, a, v, tx);
CREATE INDEX aevt ON datoms (a, e, v, tx);
CREATE INDEX avet ON datoms (a, v, e, tx);
CREATE INDEX vaet ON datoms (v, a, e, tx);
```

- A fact is current if it was asserted and not retracted later.
  A derived `current` table makes reads cheap.
- Each transaction is one SQLite transaction with `BEGIN IMMEDIATE`.
- A class-to-facts layer turns each field into a fact, and saving is a set difference between the old and new facts.
- Missing: query planning for many-clause queries, aggregates, tooling, compaction, real deletion, maturity.
- Relation checks (foreign keys, one-to-one, cascade) become Python code in the transact function.

## Queries written in Python

Design: build, compile, run.

- A query is a frozen dataclass.
  Each method returns a new query.
- A pure function compiles it to `(sql, params)`.
  Identifiers come from class metadata, values are always parameters.
- One small function runs it.

Minimal operations: from, where, select, order by and limit, group by with aggregates and having, and join (or `IN` and `EXISTS` subqueries).

Python gotchas: overloaded `==` breaks hashing, `&`, `|` and `~` bind tighter than comparisons, and chained comparisons silently lose a part.

Results come back as a declared frozen class:

```python
q = (query(Order)
     .join(Customer, o.customer_id == c.id)
     .where(o.total > 100)
     .group_by(c.city)
     .select_into(CityTotal, city=c.city, total=sum_(o.total))
     .order_by(desc(sum_(o.total)))
     .limit(10))
rows: tuple[CityTotal, ...] = run(conn, q)
```

A class built at runtime with `make_dataclass` also works, but type checkers cannot see it.

Existing libraries: SQLAlchemy Core (best fit), PyPika, sqlglot, Ibis, Polars and DuckDB, `aiosql` and `pugsql`.
Full ORMs have mutable, session-tied objects.

## What this adds over a traditional ORM

Gains:

- No hidden state.
  ORM objects are mutable and tracked by a session, which causes lazy loads from attribute access, stale identity maps and detached instance errors.
- Database calls are visible.
  Only `run`, `load` and `upsert` touch the database.
- Business logic is pure functions, so tests need no database or mocks.
- No accidental N+1 queries.
- Each query returns its own typed result class.
- No session, unit of work or change tracking.
  Saving is an explicit upsert.

Where an ORM is better:

- Maturity, many dialects, and fixes for corner cases.
- Migration tooling (Alembic, Django migrations).
- Convenient navigation like `order.customer.name`.
- Pooling, bulk operations, optimistic locking, inheritance mapping.
- Team familiarity and community answers.

The practical gap is smaller than the theory.
SQLAlchemy Core is already the non-ORM half of SQLAlchemy.
Core plus frozen classes and `select_into` gives most of the benefit and keeps the mature parts.
The value is predictability and testability, not new capabilities.

## Impact on application code of flat, immutable classes

```python
# mutable OO
class Account:
    def __init__(self, balance): self.balance = balance
    def withdraw(self, amt):
        if amt > self.balance: raise ValueError
        self.balance -= amt

acct.withdraw(50)

# flat and immutable
@dataclass(frozen=True, slots=True)
class Account:
    id: int
    balance: int

def withdraw(acct: Account, amt: int) -> Account:
    if amt > acct.balance: raise ValueError
    return replace(acct, balance=acct.balance - amt)

acct = withdraw(acct, 50)
```

What changes:

- Behavior moves out of classes into functions.
  Polymorphism uses `match`, `functools.singledispatch`, `Protocol` or a dict of functions.
- State is passed in and returned.
- Things that are really stateful (connections, clocks, caches, random generators) stay as objects and are passed as parameters.
- Updates use `replace(...)`, which is longer than `obj.x = y`.
- Identity is an `id` field.
  Equal fields means equal objects, and "same customer" means same id.
- Relationships are lookups, such as `customers[order.customer_id]`.
- Saving is an explicit call on the new value.

Gains:

- No aliasing bugs.
- Easy testing.
- Safe sharing across threads without locks.
- Easy caching, memoization and undo.
- Invariants checked at construction hold for the object's life.

New bugs and costs:

- Forgetting to rebind the result.
  `withdraw(acct, 50)` without `acct = ...` silently does nothing.
- Stale copies.
  Anyone holding the old value keeps the old balance, so decide where the single current version lives (one state value or the database).
- Copying cost for big collections, which is where persistent structures come in.
- Lost encapsulation.
  Rules across several objects cannot live in one class.
- Derived data must be computed on demand or set at construction.
  `cached_property` does not work with `slots=True`.
- Many libraries (ORMs, GUI and form frameworks) expect mutable objects.

It pays off most in code with a lot of shared state changed from many places, such as business rules and data pipelines.
It pays off least in code that is mostly local mutation.
A local mutable variable inside a function that returns an immutable result gives most of the benefit without the ceremony.

## Ideas to try next

1. Generic `load` and `upsert` on SQLite with index DDL generation, a missing-index check, and tests.
2. The `related` subquery filter and the expression objects.
3. The Bitcask-style log with checksums and replay.
4. The `datoms` table with `transact`, the current-state query, `pull` and `as-of`.
5. A tiny Datalog pattern compiler.
6. Wrap SQLAlchemy Core with a `select_into` that returns frozen result classes.
