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

A query is an immutable description, so adding a filter returns a new query.
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
from inlay import Field, Ref, entity, key


@entity(indexes=[("city",)])
class Customer:
    id: Field[str] = key()
    name: Field[str]
    city: Field[str]


@entity(indexes=[("customer",), ("total",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]


# Opens the file: .inlay.db in the current directory, or the one given, as in inlay.open("shop.db").
# The db is closed at the end of the block, like a file.
with inlay.open() as db:
    # Creates the tables and indexes of the two classes; it raises if they are there already.
    # A class goes after the classes it refers to, or in the same call.
    if Customer not in db:
        db.register(Customer, Order)

    ana = Customer(name="Ana", city="Lisbon")
    bob = Customer(name="Bob", city="Porto")
    # One call can mix classes in the same transaction, and it is all or nothing.
    # A record may refer to another one in the same call.
    db.add(ana, bob, Order(customer=ana.id, total=120), Order(customer=ana.id, total=40))

    # db is like a dict from classes to sets of records, so you access the db by class.
    # A class with no table in the file raises KeyError, as a missing key does.
    # The first use of a class checks its table, and creates or drops indexes to match the class.
    orders = db[Order]
    
    # `db[Order]` is the set of all orders, since orders are unique on id, the db behaves as a set.
    # `filter` takes ordinary Python expressions. `Order.total > 100` is not evaluated in Python: it is a condition.
    # The conditions are evaluated by the DB, not in python.
    # Notice how you can also filter on referenced class attributes.
    lazy_big_lisbon_orders = orders.filter((Order.total > 100) & (Order.customer.city == "Lisbon"))

    # `lazy_big_lisbon_orders` is a lazy query: nothing has run yet, and it can be filtered further.
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

Each field is declared `Field[...]`: on a record `order.total` is the stored `int`, and on the class `Order.total` is a value for queries, so `Order.total > 100` is a condition.
Type checkers see both readings, so they check the queries as they check the records.
A default is written `default(...)`, as in `discount: Field[int | None] = default(None)`, since type checkers would check a plain `= None` against `Field[...]`.

`Cls in db` tells whether a class has a table, and `del db[Cls]` drops it with its records, unless another table still points to it.
Several processes that start on a new file at once check and register inside `db.transaction()`, which takes the write lock first, so only one of them creates the tables.

### Optional fields

A field typed `X | None` can hold `None`, and an optional reference is written `Ref[Customer, str | None]`.

```python
from inlay import coalesce, default, negate

@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer, str | None]
    total: Field[int]
    discount: Field[int | None] = default(None)

orders = db[Order]
orders.filter(Order.discount == None)          # the orders with no discount
orders.filter(Order.discount != 5)             # includes the orders with no discount, as in Python
orders.filter(Order.discount > 1)              # a comparison with None is false, so these are left out
orders.filter(negate(Order.discount > 1))      # and here they are in
orders.filter(Order.customer.city == "Lisbon") # an order with no customer matches nothing through it
orders.filter(coalesce(Order.discount, 0) < 5) # the first value that is not None, as SQL's COALESCE
orders.sorted(key=coalesce(Order.customer.city, "unknown"))  # through an empty Ref the city counts as None
```

`coalesce` also works as the value of a projection field, and on plain values, where it is computed in Python.

Conditions follow Python, so `==` and `!=` treat `None` as a value, and a condition and its negation always split the records in two.
`one_of` can include `None`.
Sorting puts `None` first, and last in reverse.
`sum`, `min`, `max` and `avg` skip `None`, and `aggregate_by` groups the records with `None` under the key `None`.
The key cannot be optional, and other unions, such as `int | str`, are refused.
An optional reference takes a default the same way, `customer: Ref[Customer, str | None] = default(None)`.

### Enums

A field can be a `StrEnum` or an `IntEnum`. It is stored as its value and read back as a member:

```python
from enum import StrEnum

class Status(StrEnum):
    OPEN = "open"
    PAID = "paid"

@entity
class Invoice:
    id: Field[str] = key()
    status: Field[Status]

db[Invoice].filter(Invoice.status == Status.PAID)
db[Invoice].aggregate_by(Invoice.status, len)  # {Status.OPEN: 3, Status.PAID: 12}
```

A stored value that is no longer a member raises when it is read, rather than coming back as text.
Its column is the one of a `str` or an `int`, so a field that becomes an enum keeps its table, and a hashed key does not change.
Other enums are refused, since their values can be of any type.

### Keys from the values

`hashed()` instead of `key()` makes the key the SHA-256 of the other fields, so records with the same values have the same key.
It suits values that many records point to, such as an address that every night's sighting of a house repeats: `db.update(address)` stores it once.

```python
from inlay import hashed

@entity
class Address:
    id: Field[str] = hashed()
    street: Field[str]
    town: Field[str]

Address(street="Rua do Sol", town="Faro").id == Address(street="Rua do Sol", town="Faro").id  # True
```

The key is computed when a record is built, also by `replace`, and cannot be passed; type checkers still offer it.
The hash reads the values as they are stored, so a datetime gives the same key in any time zone.
Changing the fields of the class changes every key, and the records that point to them must be migrated in the same step.
A record read back from the db is restored as it was stored, so a `__post_init__` runs only when a record is built.

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

# One number per group, as a mapping in key order: {date(2026, 1, 1): 1520.0, date(2026, 2, 1): 1483.0, ...}
revenue_in_euros = invoices.aggregate_by(to_month(Invoice.at), sum, Invoice.total / 100)
invoices_per_day = invoices.aggregate_by(to_day(Invoice.at), len)
# Keyed by the customer's key, for the orders of the example above.
average_order = db[Order].aggregate_by(Order.customer, avg, Order.total)
# A value of a referenced record works too, as a key, a value or in `sorted`.
revenue_per_city = db[Order].aggregate_by(Order.customer.city, sum, Order.total)
by_customer_name = db[Order].sorted(key=Order.customer.name)

# The same functions work on plain values, so code that loops over records uses them too.
to_month(datetime(2026, 3, 5, 23, 30))  # date(2026, 3, 1)
avg([1, 2])                             # 1.5
```

`aggregate_by` takes the built-ins `len`, `sum`, `min` and `max`, and inlay's `avg` for what Python lacks.
With no values, in an empty query or a group whose values are all `None`, `sum` gives 0 and the others give `None`, or the `default` given to `min` and `max`.
Groups with no records are left out, as in a `Counter`, and a key can be a tuple of values, as in `sorted`.

What `aggregate_by` gives is a query of groups: a mapping that runs again on every read, as a query does.
`dict(totals.items())` keeps one reading, read in one query.
Groups are filtered, sorted and sliced like records, by `value` and by their keys written as they were grouped by:

```python
totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
top_five = totals.sorted(key=totals.value, reverse=True)[:5]
dict(top_five.items())                           # {"bob": 4200, "ana": 3100, ...}, biggest first
totals.filter((totals.value > 1000) & (Order.customer != ana.id))
```

A condition on a field that is not a key, such as `Order.total`, is refused: it belongs before `aggregate_by`, on the records.

Looked up with a field instead of a key, it gives each record the value of its own group, for use in queries:

```python
biggest = db[Order].aggregate_by(Order.customer, max, Order.total)
biggest_orders = db[Order].filter(Order.total == biggest[Order.customer])  # each customer's biggest order

totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
big_customers = db[Customer].filter(totals[Customer.id] > 1000)
```

It all runs in one query, and nothing is read into Python.
A key with no group gives `None`, as an empty optional `Ref` does, so a customer with no orders is not among `big_customers`.
Groups that are filtered or sliced leave out the keys they drop, so `top_five[Customer.id]` is `None` for the others.

### Queries without a db

`db[Order]` and what is built from it run on that db, but a query needs no db to be built.
`query(Order)` starts one, and `db[q]` runs it, so a query or a group can be defined once, at the top of a module:

```python
from inlay import query

big_orders = query(Order).filter(Order.total > 100).sorted(key=Order.total)
biggest = query(Order).aggregate_by(Order.customer, max, Order.total)

for order in db[big_orders]: ...
dict(db[biggest].items())
```

Reading a query that has no db, by looping over it, `len` or `in`, raises `TypeError`.
Built into another query, as a group value or in `one_of`, a query adds its conditions only, and the db that runs is the outer one.

### Your own functions

`to_month` and `avg` are not special: they are a `Function` and an `Aggregate`, and yours work the same way.
Each one is written twice, in Python for plain values and in SQL for the db, with `{}` where each argument goes.
A function takes the type of each argument as a tuple, and `(int, ...)` takes any number of ints, whose SQL then has one `{}` for all of them.
A type variable stands for the same type wherever it appears, as in Python's generics: `coalesce` takes `(T | None, ...)` and gives `T`, with `T = TypeVar("T")`.

```python
from inlay import Aggregate, Function

hour = Function("hour", (datetime,), int, lambda x: x.hour, "CAST(strftime('%H', {}) AS INTEGER)")
substr = Function("substr", (str, int, int), str, lambda s, start, n: s[start - 1:start - 1 + n], "substr({}, {}, {})")
distinct_count = Aggregate("distinct_count", lambda values: len(set(values)), "COUNT(DISTINCT {})", arg=object, result=int)

late = invoices.filter(hour(Invoice.at) >= 22)
revenue_per_hour = invoices.aggregate_by(hour(Invoice.at), sum, Invoice.total)
days_open_per_month = invoices.aggregate_by(to_month(Invoice.at), distinct_count, to_day(Invoice.at))
hour(datetime(2026, 1, 1, 23))  # 23
```

The two versions must give the same answers, and nothing checks that they do.
The SQL sees values as inlay stores them: dates as ISO 8601 text, and aware datetimes in UTC.
In the db, a function given None for an argument it takes without None gives None, as SQL's do; `coalesce`, which takes None, is how to choose a value instead.

### Projections

A projection is the shape of a result that is not a stored record, such as the rows an API returns: some fields of a record, and values from the records it refers to.
With the `Customer` and `Order` of the first example:

```python
from inlay import projection

@projection(Order)
class OrderRow:
    id: Field[str]                              # named as a field of Order, so it takes it
    total: Field[int]
    name: Field[str] = Order.customer.name      # any other field is given its value
    city: Field[str] = Order.customer.city
    with_tax: Field[float] = Order.total * 1.25

rows = db[Order].filter(Order.total > 100).project(OrderRow)
rows.filter(OrderRow.city == "Lisbon").sorted(key=OrderRow.name)[:50]
rows.aggregate_by(OrderRow.city, sum, OrderRow.with_tax)
```

A projection has one root, `Order` here, and one row for each of its records, so it must take the root's key.
Each field is typed as its value, with `| None` when the value comes through an optional `Ref`, and `@projection` raises when they differ.
Conditions on the root come before `project`, and filters, sorting, slices and aggregates on the projection's fields after it, all in one query.
`project` takes a query that is only filtered, since a subquery keeps no order.
A projection has no table, so it is not registered and `db[OrderRow]` raises.

A field can be the value of a group, defined without a db as above, such as the biggest order of each order's customer:

```python
biggest = query(Order).aggregate_by(Order.customer, max, Order.total)

@projection(Order)
class OrderWithBiggest:
    id: Field[str]
    total: Field[int]
    customers_biggest: Field[int | None] = biggest[Order.customer]
```

Its value is `None` for a key with no group, so the field is optional.

A small shop app that uses all of this, with comments that point at each feature, is in [`examples/shop.py`](examples/shop.py).

### Changing a class

There are no migration files.
A migration is a Python function that takes the db, and `db.migrate` runs it in one transaction: either all of it happens or none of it does.
It changes the shape of one table.
Here is a change that needs real computation: every order gets a `total_amount`, the sum of price times quantity over its line items.
The code is shown before the change, while the migration exists, and after it.

Before: an order has no `total_amount` yet.

```python
@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]

@entity(indexes=[("order",)])
class LineItem:
    id: Field[str] = key()
    order: Ref[Order]
    price: Field[int]
    qty: Field[int]

with inlay.open() as db:    # the table "order" has the columns id, customer
    ...
```

While: the old `Order` stays in the code under another name, because it describes the table as it is stored.
`Order` gets the new field, and the migration builds each new order from the old one and its line items.

```python
@entity(indexes=[("customer",)])
class Order:                   # the new shape
    id: Field[str] = key()
    customer: Ref[Customer]
    total_amount: Field[int]

# ---- migration: delete all of this once it has run ----
@entity(indexes=[("customer",)])
class OrderLegacy:             # the old class, renamed
    id: Field[str] = key()
    customer: Ref[Customer]

def add_total_amount(db: Db) -> None:
    for old in db[OrderLegacy]:
        lines = db[LineItem].filter(LineItem.order == old.id)
        db.add(Order(id=old.id, customer=old.customer, total_amount=lines.sum(LineItem.price * LineItem.qty)))

with inlay.open() as db:
    db.migrate(add_total_amount, OrderLegacy, Order)    # runs once, and does nothing after that
# ---- end of migration ----

with inlay.open() as db:
    ...
```

After: once the migration has run, everything between the two markers is deleted, and the new `Order` is all that is left.

```python
@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total_amount: Field[int]

with inlay.open() as db:    # the table "order" has the columns id, customer, total_amount
    ...
```

`migrate` checks that the table `order` holds the shape of `OrderLegacy`, and copies it aside to the table `order_legacy`.
Then it creates the new table `order` and runs the function, which reads the old records and can read and write any records.
At the end it drops `order_legacy`, and the commit checks that every reference points to a record.
The other tables point to the table by name, so `LineItem` points to the new orders without changing.
If the table already matches `Order`, nothing happens, so the migration can run every time the app starts until it is deleted.

The new class keeps the name of the table, and the key cannot change while other tables point to it.
So there is at most one migration for each table in the code, and a db that is behind by two migrations of the same table needs the older code first.
To rename a class, call `db.rename("Order", Purchase)` with the old name and the new class.
It renames the table, its indexes, and the references to it, and then checks the table against `Purchase`.
If the table of `Purchase` is already there and `order` is gone, nothing happens.
`migrate` and then `rename` in one `db.transaction()` change both the shape and the name, all or nothing; that pair only runs once, since the second time there is no table `order` to migrate.
A change that does not alter the schema is not a migration, just an ordinary write.

### History, an experiment

`inlay.history` is the same db, except that nothing is ever lost: it can be read as it was at any past transaction.

```python
import inlay.history

with inlay.history.open() as db:                     # the file is .inlay-history.db
    db.register(Customer, Order)                      # a transaction like any other, here number 1
    db.add(ana, order)                                # writes and reads work as above
    db.update(replace(order, total=0))

    before = db.as_of(2)                              # a transaction number, or a datetime with a timezone
    before[Order].filter(Order.total > 100)           # the same reads, as of that transaction
    before.add(order)                                 # AttributeError: a snapshot cannot be written
```

`db.tx` is the number of the latest committed transaction, and 0 before the first one.
An update or a removal ends the current version of a record and keeps it.
SQLite still checks keys and references, so a current order must refer to a current customer.
One difference from the db above: the same key twice in one `update` raises `AlreadyExists`, where inlay keeps the last record.
A class cannot be changed yet, so there is no `migrate` or `rename`.
Optional fields are refused, and so are values read through a `Ref`, such as sorting by `Order.customer.name`.
The design and its limits are in [`notes/2026-11-13-immutable-db.md`](notes/2026-11-13-immutable-db.md).

`inlay.datoms` is a second experiment with `as_of`, paused for now: it stores each field of a record as a datom, a row of one table shared by all classes, instead of one row per version.
The library checks keys and references itself, since that table has no constraints, and an update stores only the fields that change.
Reads of the present use a table of the current values, and a snapshot fills a temporary one on its first read.
It still takes its classes in `open`, as in `inlay.datoms.open([Customer, Order])`, since it has no table for each class to register.
Like `inlay.history`, it refuses optional fields and values read through a `Ref`.

## Design

**Records are values.**
A record is a frozen dataclass, like a tuple with named fields.
To change one, make a new one with `replace` and store it.
There is no session, no object tracking, and no lazy loading.
Nothing changes behind the back of the code that holds a record.

**The db is a set of records per class.**
The method names come from Python's own types: `add`, `update`, `remove`, `discard`, `clear`, `in`, `len`, `sorted`, slicing, `sum`, `min` and `max`.
Reads return whole records, never single fields.
Database calls are always visible: only `db`, and the sets and queries it hands out, touch the database.

**Relations are keys.**
`customer: Ref[Customer]` is stored as the customer's key.
On a record it is a plain `str`, or `str | None` for a `Ref[Customer, str | None]`, and following it is an explicit `db[Customer].get(order.customer)`.
On the class it leads to the other record's fields, so a filter can use `Order.customer.city`.
Type checkers see both readings.

**Conditions are values.**
`Order.total > 100` builds a small tree.
A pure function turns it into SQL, and every value goes in as a parameter:

```python
Order.customer.city == "Lisbon"
# "customer" IN (SELECT "id" FROM "customer" AS "_0" WHERE "city" IS ?)
```

`==` becomes SQLite's `IS`, which treats NULL as a value and uses indexes as `=` does, and `negate` counts an unknown result as false first, so conditions keep Python's two values.

Relations become subqueries, so there are no joins to write and no duplicate rows.
`filter` takes one condition. Combine conditions with `&`, `|` and `negate`, and a list of them with `all_of` and `any_of`.
Python's `and`, `or`, `not` and `1 < x < 5` would silently drop part of a condition, so a condition refuses to be a boolean and names the right tool.

**Queries are lazy and immutable.**
`filter`, `sorted`, slicing, `project` and `aggregate_by` return a new query, and no SQL runs until it is read.
A query is a tree of plain data; the db it may hold is only where it runs, and `db[q]` gives it another.
Every read runs the query again, so it sees current data, and `tuple(q)` gives a snapshot.
`len`, `sum`, `min`, `max` and `aggregate_by` run inside the db and load no records.
A slice is a query too, so `q[:10].filter(...)` filters those ten records.
Changing a table while looping over it is refused, because SQLite does not say what such a loop returns.

**Nothing is implicit.**
Indexes are declared on the class, and a `Ref` must be the first field of one, because SQLite does not index foreign keys on its own.
Tables are `STRICT`.
Tables are only created by `register`, never by a read or a write.
When a class no longer matches its table, its first use raises `SchemaMismatch` and changes nothing; only its indexes follow the class by themselves.

**Integrity is checked for the caller.**
References are checked when a call or a transaction ends, so the order of writes never matters.
A reference can never point to nothing: that raises `BrokenRef` and undoes the call.
Each write call is atomic, and `db.transaction()` groups several, with savepoints when nested.

**The core is pure.**
Building conditions, compiling them, and building SQL statements are pure functions.
Only the `db` modules, `inlay.db`, `inlay.history.db` and `inlay.datoms.db`, talk to SQLite.

| Module | Job |
|---|---|
| `meta` | the metadata about each class and its fields |
| `entity` | the `@entity` and `@projection` decorators, `key` and `Ref` |
| `nodes` | the condition and query trees, as plain data |
| `conditions` | field references, the builder that turns `Order.total > 100` into the tree |
| `functions` | functions such as `to_month` and aggregates such as `avg`, each with its Python and SQL |
| `types` | Python types to SQLite types, and back |
| `sql` | pure functions that build one SQL statement or clause each |
| `db` | `open`, the db with `register`, `migrate` and `rename`, record sets, queries and groups, and transactions |

## What it does not do

This is version one, and the limits are on purpose.

- SQLite only.
- A `Db` works only in the thread and process that opened it: open one in each.
- No composite keys.
- No cascading deletes or reverse relations.
- Few functions so far: arithmetic, `to_month` and `to_day`.
- Slices that skip many records are slow, because SQLite reads and discards the skipped rows.
  Filtering from the last seen value avoids that.

## Running it

inlay needs Python 3.15 or later.

```
uv run pytest                          # tests
uv run pyright                         # type checks, including the example and the benchmark
uv run python examples/shop.py         # the example app
uv run python benchmarks/history.py    # inlay against both history experiments, now and at past transactions
```

## Notes

- [`notes/2026-11-10-design-v1.md`](notes/2026-11-10-design-v1.md) is the full design of inlay, with the reasons for each choice, and its follow-ups.
- [`notes/2026-11-11-sales-example-learnings.md`](notes/2026-11-11-sales-example-learnings.md) is what writing the sales example taught us about inlay, and the ideas it suggests.
- [`notes/2026-11-13-migrations.md`](notes/2026-11-13-migrations.md) is the design of `migrate` and `rename`, and the discussion that led to it.
- [`notes/2026-11-13-roadmap.md`](notes/2026-11-13-roadmap.md) is what is open and what comes next.
- [`notes/2026-11-13-immutable-db.md`](notes/2026-11-13-immutable-db.md) is the design of the two history experiments, `inlay.history`, one table of versions per class, and `inlay.datoms`, one table of datoms, and the plans for schema changes.
- [`notes/2026-11-21-open-without-classes.md`](notes/2026-11-21-open-without-classes.md) is the design for opening a db without a list of classes, with `db.register`.
- [`notes/2026-11-21-projections.md`](notes/2026-11-21-projections.md) is a design for projections, read-only classes defined by expressions over one stored class, not built yet.
- [`notes/2026-11-10-immutable-data-and-databases.md`](notes/2026-11-10-immutable-data-and-databases.md) is the background: immutable data, FP style, and database design in Python.
