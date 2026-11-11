# immutable-data-lab

Notes and small experiments on immutable data, functional style in Python, and ways to store data without writing SQL all over the code.

The main experiment is `inlay`, a small library in `src/inlay`.

## inlay

inlay is a set-like store for frozen dataclasses, on SQLite.
Classes are flat and immutable, and records are read and written without any SQL in the application code.

```python
from dataclasses import replace

import inlay
from inlay import Ref, entity, key


@entity(indexes=[("city",)])
class Customer:
    id: str = key()
    name: str
    city: str


@entity(indexes=[("customer",), ("total",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


with inlay.open("shop.db", [Customer, Order]) as db:
    ana = Customer(name="Ana", city="Lisbon")
    with db.transaction():
        db.add(ana, Order(customer=ana.id, total=120))

    big = db[Order].filter(Order.total > 100, Order.customer.city == "Lisbon")
    for order in big.sorted(key=Order.total, reverse=True)[:10]:
        print(order)

    order = next(iter(big))
    db.update(replace(order, total=150))
    db[Order].clear(Order.total == 0)
```

A longer tour, with every step explained and checked, is in [`examples/shop.py`](examples/shop.py).

## Design

**Records are values.**
A record is a frozen dataclass, like a tuple with named fields.
To change one, make a new one with `replace` and store it.
There is no session, no object tracking, and no lazy loading.
Nothing changes behind the back of the code that holds a record.

**The db is a set of records per class.**
The method names come from Python's own types: `add`, `update`, `remove`, `discard`, `clear`, `in`, `len`, `sorted`, slicing, `sum`, `min` and `max`.
Reads return whole records, never single fields.
Database calls are always visible: only `db`, and the sets and views it hands out, touch SQLite.

**Relations are keys.**
`customer: Ref[Customer]` is stored as the customer's key.
On a record it is a plain `str`, and following it is an explicit `db[Customer].get(order.customer)`.
On the class it leads to the other record's fields, so a filter can use `Order.customer.city`.
Type checkers see both readings.

**Conditions are values.**
`Order.total > 100` builds a small tree.
A pure function turns it into SQL, and every value goes in as a parameter:

```python
Order.customer.city == "Lisbon"
# "customer_id" IN (SELECT "id" FROM "customer" WHERE "city" = ?)
```

Relations become subqueries, so there are no joins to write and no duplicate rows.
Combine conditions with `&`, `|`, `all_of`, `any_of`, `negate` and `none_of`.
Python's `and`, `or`, `not` and `1 < x < 5` would silently drop part of a condition, so a condition refuses to be a boolean and names the right tool.

**Views are lazy and immutable.**
`filter`, `sorted` and slicing return a new view, and no SQL runs until it is read.
Every read runs the query again, so it sees current data, and `tuple(view)` gives a snapshot.
`len`, `sum`, `min` and `max` run inside SQLite and load no records.
A slice is a view too, so `view[:10].filter(...)` filters those ten records.
Changing a table while looping over it is refused, because SQLite does not say what such a loop returns.

**Nothing is implicit.**
Indexes are declared on the class, and a `Ref` must be the first field of one, because SQLite does not index foreign keys on its own.
Tables are `STRICT`.
When a class no longer matches its table, `open` raises `SchemaMismatch` and changes nothing.

**Integrity is checked for the caller.**
References are checked when a call or a transaction ends, so the order of writes never matters.
A reference can never point to nothing: that raises `BrokenRef` and undoes the call.
Each write call is atomic, and `db.transaction()` groups several, with savepoints when nested.

**The core is pure.**
Building conditions, compiling them, and building SQL statements are pure functions.
Only the `db` module talks to SQLite.

| Module | Job |
|---|---|
| `entity` | the `@entity` decorator, `key`, `Ref`, and the metadata about each class |
| `conditions` | fields, relation paths, and the condition tree |
| `types` | Python types to SQLite types, and back |
| `sql` | pure functions that build one SQL statement or clause each |
| `db` | `open`, the db, record sets and views, and transactions |

## What it does not do

This is version one, and the limits are on purpose.

- SQLite only, and one thread.
- No optional fields, enums or composite keys.
- No cascading deletes, reverse relations, grouping, or migrations.
- Slices that skip many records are slow, because SQLite reads and discards the skipped rows.
  Filtering from the last seen value avoids that.
- Typing class-level fields as their value type costs a lot of code, and the design notes list it as something to re-evaluate.

## Running it

inlay needs Python 3.15 or later.

```
uv run pytest                      # tests
uv run pyright                     # type checks, including the example
uv run python examples/shop.py     # the guided tour
```

## Notes

- [`notes/2026-11-10-design-v1.md`](notes/2026-11-10-design-v1.md) is the full design of inlay, with the reasons for each choice, and its follow-ups.
- [`notes/2026-11-10-immutable-data-and-databases.md`](notes/2026-11-10-immutable-data-and-databases.md) is the background: immutable data, FP style, and database design in Python.
