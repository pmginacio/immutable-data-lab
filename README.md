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
Type checkers, refactoring tools and tests cannot see inside it.
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
The objects they return carry hidden machinery too: sessions, identity maps, change tracking and lazy loading.

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
After the session is closed, reading an attribute that was not loaded yet raises `DetachedInstanceError`, because the object was never a plain value.

The object is secretly a window onto a row.
In inlay a record is a frozen value: assigning to a field raises an error, a write is always a call such as `db.update(...)`, and a record keeps working after the db is closed.

### Where it comes from

Classes written in the object-oriented style are built for information hiding, inheritance and mutation.
Stored data is the opposite: it is plain values, laid out openly, in rows that have keys.
The two do not fit, so something has to translate between them.

In plain code the translation is SQL metaprogramming: strings of SQL, written and put together by hand.
In an ORM it is the mapping layer: sessions that track which objects changed, identity maps, lazy loading, and rules for storing a class hierarchy in tables.
The mini-languages and the machinery are consequences of this mismatch.

### Changing the classes, not the translator

Two choices remove most of the mismatch.

- Immutable values instead of mutable objects.
  A class is a frozen dataclass: equal when its fields are equal, and never changed in place.
  A change is a new value.
- Flat classes instead of inheritance and nesting.
  One class is one table, and a relation is a key that points to another class.

With these, a class already has the shape of a table.
Fields are columns, the key field is the primary key, and a reference field is a foreign key.
There is nothing left to map, so no mapping layer is needed.

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

A view is a value, so adding a filter is just assigning a new view.
Nothing is put together as text, and nothing runs until the last line.
The function returns `Order` records, not tuples, and type checkers flag a misspelled field.

The price is real but small for ordinary application data.
Information hiding, inheritance, nested objects, in-place mutation and arbitrary queries are given up.
A record is plain, visible data, and the rules that were methods become functions.
A set of records is filtered, sorted and sliced like a collection held in memory, and nobody writes a query language against a list.
Joins and grouping are plain Python over whole records.

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


# Opens the file and creates the tables and indexes for these two classes
# The db is closed at the end of the block, like a file.
with inlay.open("shop.db", [Customer, Order]) as db:
    ana = Customer(name="Ana", city="Lisbon")
    bob = Customer(name="Bob", city="Porto")
    # One call can mix classes, and it is all or nothing: a record may refer to another one in the same call.
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

A small shop app that uses all of this, with comments that point at each feature, is in [`examples/shop.py`](examples/shop.py).

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
uv run python examples/shop.py     # the example app
```

## Notes

- [`notes/2026-11-10-design-v1.md`](notes/2026-11-10-design-v1.md) is the full design of inlay, with the reasons for each choice, and its follow-ups.
- [`notes/2026-11-10-immutable-data-and-databases.md`](notes/2026-11-10-immutable-data-and-databases.md) is the background: immutable data, FP style, and database design in Python.
