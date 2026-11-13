# immutable-data-lab

Notes and small experiments on immutable data, functional style in Python, and ways to store data without writing SQL all over the code.

The main experiment is `inlay`, a small library in `src/inlay`.

## Why

Code like this is everywhere:

```python
@dataclass
class Order:
    id: str
    customer_id: str
    total: int


def load_orders(conn: sqlite3.Connection, city: str | None = None, minimum: int | None = None) -> list[Order]:
    sql = """
        SELECT o.id, o.customer_id, o.total
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
    """
    conditions, params = [], []
    if city is not None:
        conditions.append("c.city = ?")
        params.append(city)
    if minimum is not None:
        conditions.append("o.total > ?")
        params.append(minimum)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY o.total DESC LIMIT 10"
    return [Order(*row) for row in conn.execute(sql, params)]


def report(conn: sqlite3.Connection) -> None:
    for order in load_orders(conn, city="Lisbon", minimum=100):
        print(order.id, order.total)
```

The middle of `load_orders` is SQL metaprogramming: a Python program that writes a program in another language, as a string.
As soon as a filter is optional, the text has to be built piece by piece, and the placeholders and the parameters have to be kept in step by hand.
Type checkers and refactoring tools cannot see inside it, and nothing checks the string before it runs.
The rows come back as tuples, so the columns in the string and the fields of the class are matched by position.

ORMs hide the string, but the replacement is often just as much a language of its own, written with Python syntax:

```python
# Django
Order.objects.filter(customer__city="Lisbon", total__gt=100).order_by("-total")[:10]

# SQLAlchemy
select(Order).join(Order.customer).where(Customer.city == "Lisbon", Order.total > 100).order_by(Order.total.desc()).limit(10)
```

`customer__city` and `total__gt` are conditions packed into names.
The SQLAlchemy line is SQL, one clause per method call.
Each is a mini-language that has to be learned next to Python.
The objects they return carry hidden machinery too: SQLAlchemy's carry sessions, identity maps and change tracking, and Django's load related rows lazily.

### Where ORMs fall short

The objects an ORM returns look like plain objects, but each one is tied to a session.
That causes problems the code does not show, such as writes that no line of code makes:

```python
order = session.get(Order, order_id)
order.total = 0
...
session.commit()
```

In SQLAlchemy, assigning to an attribute is a database write, done at the next flush.
No line says "write", so a stray assignment in a helper function changes the database.
After a commit or after the session is closed, reading an attribute that has expired or was never loaded raises `DetachedInstanceError`, because the object was never a plain value.

Both problems have the same root: the ORM tries to keep a mutable object in sync with a row automatically.
That takes machinery, and the machinery is where it fails:

- Change tracking. The session has to notice every assignment and decide when to write it. With autoflush, an ordinary query in the middle of a function first writes the pending changes. An error from an earlier assignment then shows up on an unrelated line.
- Sessions. An object belongs to one session. Attaching it to another one raises an error, so objects cannot just be passed around.

The object is secretly a window onto a row.
In inlay a record is a frozen value: assigning to a field raises an error, a write is always a call such as `db.update(...)`, and a record keeps working after the db is closed.

### Where it comes from

Classes written in the object-oriented style are built for information hiding, inheritance and mutation.
Stored data is the opposite: it is plain values, laid out openly, in rows that have keys.
The two do not fit, so something has to translate between them.

In plain code the translation is SQL metaprogramming: strings of SQL, written and put together by hand.
In an ORM it is the mapping layer: sessions that track which objects changed, identity maps, lazy loading, and rules for storing a class hierarchy in tables.
These come from mutable objects.

The query mini-languages have a second cause.
An ORM has to run any query the database can: joins, grouping, aggregates, subqueries.
Python has no syntax for those, so each ORM invents one.

### Changing the classes, not the translator

Two choices remove most of the mismatch.

- Immutable values instead of mutable objects.
  A class is a frozen dataclass: equal when its fields are equal, and never changed in place.
  A change is a new value.
- Flat classes instead of inheritance and nesting.
  One class is one table, and a relation is a key that points to another class.

With these, a class already has the shape of a table.
Fields are columns, the key field is the primary key, and a reference field is a foreign key.
The little that is left to map, such as column names and date types, is read from the class and keeps no state.
There are no sessions or identity maps, and nothing to configure beyond the class itself and its indexes.

The database is then a set of values for each class, and its API can use words that Python already has: `add`, `in`, `len`, `sorted`, slicing and `replace`.
The same function becomes:

```python
def load_orders(db: Db, city: str | None = None, minimum: int | None = None) -> list[Order]:
    orders = db[Order]
    if city is not None:
        orders = orders.filter(Order.customer.city == city)
    if minimum is not None:
        orders = orders.filter(Order.total > minimum)
    return list(orders.sorted(key=Order.total, reverse=True)[:10])
```

A view is an immutable description of a query, so adding a filter returns a new view.
Nothing is put together as text, and nothing runs until the last line.
The function returns `Order` records, not tuples, and type checkers flag a misspelled field.

The price is real.
Information hiding, inheritance, nested objects and in-place mutation are given up, and so are arbitrary queries.
A record is plain, visible data, and the rules that were methods become functions.
Code that holds records in memory does not query them.
It filters, sorts, slices and loops.
inlay offers those operations on a set of records, so its API can stay close to Python.
Joins are done in Python over the records you load, as they would be for a list.
That keeps the API small.
Reports that add up many records use `aggregate_by`, which the db computes without loading them.

## inlay

inlay is a set-like store for frozen dataclasses, backed by a mainstream database (SQLite) and with an idiomatic Python API.
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


# Opens the file and creates the tables and indexes for these two classes if needed.
# The file is .inlay.db in the current directory, or the one given with path="shop.db".
# The db is closed at the end of the block, like a file.
with inlay.open([Customer, Order]) as db:
    ana = Customer(name="Ana", city="Lisbon")
    bob = Customer(name="Bob", city="Porto")
    # One call can mix classes in the same transaction, and it is all or nothing.
    # A record may refer to another one in the same call.
    db.add(ana, bob, Order(customer=ana.id, total=120), Order(customer=ana.id, total=40))

    # db is a dict[type, Set] so that you can access the db by class type.
    # Only the classes passed to `inlay.open` are valid keys.
    orders = db[Order]
    
    # `db[Order]` is the set of all orders, since orders are unique on id, the db behaves as a set.
    # `filter` takes ordinary Python expressions. `Order.total > 100` is not evaluated in Python: it is a condition.
    # The conditions are evaluated by the DB, not in python.
    # Notice how you can also filter on referenced class attributes.
    lazy_big_lisbon_orders = orders.filter(Order.total > 100, Order.customer.city == "Lisbon")

    # `lazy_big_lisbon_orders` is a lazy view: still no query has run, and it can be filtered further.
    # Sorting and slicing are added to the same query.
    # The query runs here, in the loop, and the records arrive one at a time.
    for order in lazy_big_lisbon_orders.sorted(key=Order.total, reverse=True)[:10]:
        print(order)

    # A transaction groups several calls: if anything in the block raises, none of it is written.
    # Here Ana is merged into Bob: her orders move to him and she is deleted, or nothing changes at all.
    with db.transaction():
        db.update(*(replace(order, customer=bob.id) for order in db[Order].filter(Order.customer == ana.id)))
        db[Customer].delete(ana.id)
```

### Computed values and reports

Values can be computed, and the db does the work, so reports add up many records without loading them.
With a class `Invoice` that has a `datetime` field `at` and an `int` field `total`:

```python
from inlay import avg, to_day, to_month

invoices = db[Invoice]

# Arithmetic and functions such as `to_month` work wherever a field does: in filters, sorts and aggregates.
big = invoices.filter(Invoice.total * 0.9 > 100)
by_month = invoices.sorted(key=(to_month(Invoice.at), Invoice.total))
latest_month = invoices.max(to_month(Invoice.at))  # a date

# One number per group, as a dict in key order: {date(2026, 1, 1): 1520.0, date(2026, 2, 1): 1483.0, ...}
revenue_in_euros = invoices.aggregate_by(to_month(Invoice.at), sum, Invoice.total / 100)
invoices_per_day = invoices.aggregate_by(to_day(Invoice.at), len)
# Keyed by the customer's key, for the orders of the example above.
average_order = db[Order].aggregate_by(Order.customer, avg, Order.total)

# The same functions work on plain values, so code that loops over records uses them too.
to_month(datetime(2026, 3, 5, 23, 30))  # date(2026, 3, 1)
avg([1, 2])                             # 1.5
```

`aggregate_by` takes the built-ins `len`, `sum`, `min` and `max`, and inlay's `avg` for what Python lacks.
Groups with no records are left out, as in a `Counter`, and a key can be a tuple of values, as in `sorted`.

### Your own functions

`to_month` and `avg` are not special: they are a `Function` and an `Aggregate`, and yours work the same way.
Each one is written twice, in Python for plain values and in SQL for the db, with `{}` where the argument goes:

```python
from inlay import Aggregate, Function

hour = Function("hour", datetime, int, lambda x: x.hour, "CAST(strftime('%H', {}) AS INTEGER)")
distinct_count = Aggregate("distinct_count", lambda values: len(set(values)), "COUNT(DISTINCT {})", arg=object, result=int)

late = invoices.filter(hour(Invoice.at) >= 22)
revenue_per_hour = invoices.aggregate_by(hour(Invoice.at), sum, Invoice.total)
days_open_per_month = invoices.aggregate_by(to_month(Invoice.at), distinct_count, to_day(Invoice.at))
hour(datetime(2026, 1, 1, 23))  # 23
```

The two versions must give the same answers, and nothing checks that they do.
The SQL sees values as inlay stores them: dates as ISO 8601 text, and aware datetimes in UTC.
A function takes one argument for now.

A small shop app that uses all of this, with comments that point at each feature, is in [`examples/shop.py`](examples/shop.py).

### Changing a class

There are no migration files.
A migration is a Python function that takes the db, and `migrate` runs it in one transaction: either all of it happens or none of it does.
It changes the schema of one class.
Here is a change that needs real computation: every order gets a `total_amount`, the sum of price times quantity over its line items.
The code is shown before the change, while the migration exists, and after it.

Before: an order has no `total_amount` yet.

```python
@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]

@entity(indexes=[("order",)])
class LineItem:
    id: str = key()
    order: Ref[Order]
    price: int
    qty: int

with inlay.open([Customer, Order, LineItem]) as db:    # the table "order" has the columns id, customer
    ...
```

While: the old `Order` stays in the code under another name, because it describes the table as it is stored.
`Order` gets the new field, and the migration builds each new order from the old one and its line items.
`LineItem` does not change, but the function reads it, so it is passed as `other_entities`.

```python
@entity(indexes=[("customer",)])
class Order:                   # the new shape
    id: str = key()
    customer: Ref[Customer]
    total_amount: int

# ---- migration: delete all of this once it has run ----
@entity(indexes=[("customer",)])
class OrderLegacy:             # the old class, renamed
    id: str = key()
    customer: Ref[Customer]

def add_total_amount(db: Db) -> None:
    for old in db[OrderLegacy]:
        lines = db[LineItem].filter(LineItem.order == old.id)
        db.add(Order(id=old.id, customer=old.customer, total_amount=lines.sum(LineItem.price * LineItem.qty)))

inlay.migrate(add_total_amount, OrderLegacy, Order, other_entities=[LineItem])    # runs once, and does nothing after that
# ---- end of migration ----

with inlay.open([Customer, Order, LineItem]) as db:
    ...
```

After: once the migration has run, everything between the two markers is deleted, and the new `Order` is all that is left.

```python
@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total_amount: int

with inlay.open([Customer, Order, LineItem]) as db:    # the table "order" has the columns id, customer, total_amount
    ...
```

`migrate` sees that the table `order` still holds the old shape, moves it aside, creates the new table, and runs the function, which can read and write any records.
Then it points the foreign keys of the other tables to the new table, drops the old one, and checks that the classes match their tables and that every reference points to a record.
If the db already has the new table, nothing happens, so running it again is safe.
The file must exist and hold the old table; otherwise `migrate` raises.
Like `open`, `migrate` and `rename` use `.inlay.db` unless given another `path`.

The new class can also have a name of its own: `inlay.migrate(fn, Order, Purchase)` moves the data to a new table `purchase`, and the foreign keys of the other tables follow.
The function gets a db with the old class, the new class, and the `other_entities`.
To only rename a class, with no change to its fields, call `inlay.rename("Order", "Purchase")`: it renames the table, its indexes, and the references to it.
A change that does not alter the schema is not a migration, just an ordinary write.

## Design

**Records are values.**
A record is a frozen dataclass, like a tuple with named fields.
To change one, make a new one with `replace` and store it.
There is no session, no object tracking, and no lazy loading.
Nothing changes behind the back of the code that holds a record.

**The db is a set of records per class.**
The method names come from Python's own types: `add`, `update`, `remove`, `discard`, `clear`, `in`, `len`, `sorted`, slicing, `sum`, `min` and `max`.
Reads return whole records, never single fields.
Database calls are always visible: only `db`, and the sets and views it hands out, touch the database.

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
# "customer" IN (SELECT "id" FROM "customer" WHERE "city" = ?)
```

Relations become subqueries, so there are no joins to write and no duplicate rows.
Combine conditions with `&`, `|`, `all_of`, `any_of`, `negate` and `none_of`.
Python's `and`, `or`, `not` and `1 < x < 5` would silently drop part of a condition, so a condition refuses to be a boolean and names the right tool.

**Views are lazy and immutable.**
`filter`, `sorted` and slicing return a new view, and no SQL runs until it is read.
Every read runs the query again, so it sees current data, and `tuple(view)` gives a snapshot.
`len`, `sum`, `min`, `max` and `aggregate_by` run inside the db and load no records.
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
| `meta` | the metadata about each class and its fields |
| `entity` | the `@entity` decorator, `key` and `Ref` |
| `nodes` | the condition and query trees, as plain data |
| `conditions` | field references, the builder that turns `Order.total > 100` into the tree |
| `functions` | functions such as `to_month` and aggregates such as `avg`, each with its Python and SQL |
| `types` | Python types to SQLite types, and back |
| `sql` | pure functions that build one SQL statement or clause each |
| `db` | `open`, `migrate` and `rename`, the db, record sets and views, and transactions |

## What it does not do

This is version one, and the limits are on purpose.

- SQLite only.
- A `Db` works only in the thread and process that opened it: open one in each.
- Several processes creating the same new db file at once can fail: create it once first.
- No optional fields, enums or composite keys.
- No cascading deletes or reverse relations.
- Few functions so far: arithmetic, `to_month` and `to_day`.
- Slices that skip many records are slow, because SQLite reads and discards the skipped rows.
  Filtering from the last seen value avoids that.

## Running it

inlay needs Python 3.15 or later.

```
uv run pytest                      # tests
uv run pyright                     # type checks, including the example
uv run python examples/shop.py     # the example app
```

## Notes

- [`notes/2026-11-10-design-v1.md`](notes/2026-11-10-design-v1.md) is the full design of inlay, with the reasons for each choice, and its follow-ups.
- [`notes/2026-11-11-sales-example-learnings.md`](notes/2026-11-11-sales-example-learnings.md) is what writing the sales example taught us about inlay, and the ideas it suggests.
- [`notes/2026-11-13-migrations.md`](notes/2026-11-13-migrations.md) is the design of `migrate` and `rename`, and the discussion that led to it.
- [`notes/2026-11-13-roadmap.md`](notes/2026-11-13-roadmap.md) is what is open and what comes next.
- [`notes/2026-11-13-immutable-db.md`](notes/2026-11-13-immutable-db.md) is the discussion of an immutable, append-only db and a db of facts, as experiments next to inlay.
- [`notes/2026-11-10-immutable-data-and-databases.md`](notes/2026-11-10-immutable-data-and-databases.md) is the background: immutable data, FP style, and database design in Python.
