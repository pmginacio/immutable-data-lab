"""A tour of inlay, through a small shop.

Run it from the repository root:

    uv run python examples/shop.py

It uses a temporary database file and prints each result next to the code that makes it.
The `show` helper also checks every result, so the output cannot drift away from what the comments say.
Read it top to bottom: each numbered part builds on the one before.
"""

import tempfile
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path

import inlay
from inlay import (
    AlreadyExists,
    BrokenRef,
    Missing,
    Ref,
    SchemaMismatch,
    all_of,
    any_of,
    contains,
    entity,
    key,
    negate,
    one_of,
)

# 1. Data is plain, frozen dataclasses.
#
# `@entity` turns a class into a frozen, slotted, keyword-only dataclass and tells inlay how to store it.
# There is no base class, no session and no mutable state: a record is a value, like a tuple.
#
# `key()` marks the one field that identifies a record.
# It is filled in when the object is created (a time-ordered UUID), so the key is known before the record is stored.
#
# `indexes` lists every index you want, as tuples of field names.
# Nothing is indexed behind your back, except the key.
# A `Ref` field must be the first field of an index, because SQLite does not index foreign keys on its own.


@entity(indexes=[("city",)])
class Customer:
    id: str = key()
    name: str
    city: str


# `Ref[Customer]` is a reference to a customer, stored as the customer's key.
# On a record, `order.customer` is that key, a plain `str`.
# On the class, `Order.customer` leads to the customer's fields, so `Order.customer.city` can be used in a filter.
# Type checkers understand both readings.
@entity(indexes=[("customer",), ("total",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    status: str
    total: int
    placed: datetime


@entity(indexes=[("order",)])
class OrderLine:
    id: str = key()
    order: Ref[Order]
    product: str
    qty: int


# Business rules are plain functions from a value to a new value.
# They need no database, so they are easy to test.
def mark_paid(order: Order) -> Order:
    return replace(order, status="paid")


def section(title: str) -> None:
    print(f"\n{title}")


def show(label: str, actual: object, expected: object) -> None:
    """Print a result, and fail if it is not the one the comment above it promises."""
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"
    print(f"  {label}: {actual}")


def ids(records: Iterable[Customer | Order | OrderLine]) -> list[str]:
    """The keys of some records, in a fixed order, so results are easy to compare."""
    return sorted(r.id for r in records)


@contextmanager
def refused(error: type[Exception]) -> Iterator[None]:
    """Run a block that must fail with `error`, and print what the library says."""
    try:
        yield
    except error as e:
        print(f"  {error.__name__}: {str(e).splitlines()[0]}")
    else:
        raise AssertionError(f"expected {error.__name__}")


def main() -> None:
    section("1. Records are frozen values")
    ana = Customer(name="Ana", city="Lisbon")
    show("the key was generated, a UUID version", uuid.UUID(ana.id).version, 7)

    with refused(FrozenInstanceError):
        ana.city = "Porto"  # type: ignore

    # To change a record you make a new one. `replace` keeps the key.
    moved = replace(ana, city="Porto")
    show("a copy keeps the key", moved.id == ana.id, True)
    show("and the original did not change", ana.city, "Lisbon")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "shop.db"
        run(path)
        reopen(path)


def run(path: Path) -> None:
    # 2. Open a database.
    #
    # Pass the classes it holds. Missing tables and indexes are created, all in one transaction.
    # `db` is used like a `shelve`: with a `with` block, so the file is closed at the end.
    section("2. Opening a database")
    with inlay.open(path, [Customer, Order, OrderLine]) as db:
        show("the database is one file", path.exists(), True)

        # 3. Add records.
        #
        # `db.add` takes any mix of classes. Each call is atomic: if one record fails, none is written.
        # References are checked when the call ends, not row by row.
        # So the order does not matter, and orders can come before the customers they refer to.
        section("3. Adding records")
        customers = [
            Customer(id="ana", name="Ana", city="Lisbon"),
            Customer(id="bob", name="Bob", city="Porto"),
            Customer(id="cid", name="Cid", city="Lisbon"),
            Customer(id="dee", name="Dee", city="Faro"),
        ]
        orders = [
            Order(id="o1", customer="ana", status="paid", total=120, placed=datetime(2026, 1, 5)),
            Order(id="o2", customer="ana", status="new", total=40, placed=datetime(2026, 2, 10)),
            Order(id="o3", customer="bob", status="new", total=200, placed=datetime(2026, 2, 11)),
            Order(id="o4", customer="cid", status="paid", total=75, placed=datetime(2026, 3, 1)),
            Order(id="o5", customer="dee", status="new", total=15, placed=datetime(2026, 3, 2)),
        ]
        lines = [
            OrderLine(id="l1", order="o1", product="keyboard", qty=1),
            OrderLine(id="l2", order="o1", product="mouse", qty=2),
            OrderLine(id="l3", order="o3", product="monitor", qty=1),
            OrderLine(id="l4", order="o4", product="cable", qty=3),
        ]
        db.add(*orders, *lines, *customers)
        show("orders stored", len(db[Order]), 5)

        # 4. Read records.
        #
        # `db[Customer]` is a set of customer records. It looks like a `set`, and its reads work the same way.
        # `get` finds a record by key. `in` needs an equal record, not only the same key.
        # Reads always return whole records, never single fields.
        section("4. Reading records")
        show("get by key", db[Customer].get("ana"), customers[0])
        show("get a key that is not there", db[Customer].get("nobody"), None)
        show("a stored record is in the set", customers[0] in db[Customer], True)
        show("a changed copy is not", replace(customers[0], city="Porto") in db[Customer], False)
        show("iterating gives records", ids(db[Customer]), ["ana", "bob", "cid", "dee"])

        # 5. Conditions are values.
        #
        # `Order.total > 100` does not compute anything. It builds a small tree that describes the test.
        # `filter` turns that tree into SQL, passing every value as a parameter, so there is no string to inject into.
        # A field of a related record is reached with a dot: no join to write, and no duplicate rows.
        section("5. Conditions")
        show(
            "orders of customers in Lisbon",
            ids(db[Order].filter(Order.customer.city == "Lisbon")),
            ["o1", "o2", "o4"],
        )
        # Relations can be followed as far as you like.
        show(
            "order lines of customers in Lisbon",
            ids(db[OrderLine].filter(OrderLine.order.customer.city == "Lisbon")),
            ["l1", "l2", "l4"],
        )
        # Several conditions in one `filter` are combined with "and".
        show(
            "paid orders over 100",
            ids(db[Order].filter(Order.status == "paid", Order.total > 100)),
            ["o1"],
        )
        # `&`, `|` and `negate` combine conditions. Python needs the parentheses around each comparison.
        show("big or tiny", ids(db[Order].filter((Order.total > 150) | (Order.total < 20))), ["o3", "o5"])
        show("not paid", ids(db[Order].filter(negate(Order.status == "paid"))), ["o2", "o3", "o5"])
        # `all_of` and `any_of` are the built-ins `all` and `any` for conditions.
        show(
            "placed in February, and new",
            ids(
                db[Order].filter(
                    all_of(Order.placed >= datetime(2026, 2, 1), Order.placed < datetime(2026, 3, 1)),
                    Order.status == "new",
                )
            ),
            ["o2", "o3"],
        )
        show("name has an o", ids(db[Customer].filter(contains(Customer.name, "o"))), ["bob"])
        show(
            "name starts with A or B",
            ids(db[Customer].filter(any_of(Customer.name.startswith("A"), Customer.name.startswith("B")))),
            ["ana", "bob"],
        )
        show(
            "lives in Porto or Faro",
            ids(db[Customer].filter(one_of(Customer.city, ("Porto", "Faro")))),
            ["bob", "dee"],
        )

        # 6. Views are lazy and immutable.
        #
        # `filter`, `sorted` and slicing return a new view. No SQL runs until you read it.
        # A view is a value: keep it, pass it around, build on it. Every read runs the query again, so it sees current data.
        section("6. Views")
        new_orders = db[Order].filter(Order.status == "new")
        show("new orders", len(new_orders), 3)
        snapshot = tuple(new_orders)  # `tuple` gives a copy that does not change
        extra = Order(id="o6", customer="bob", status="new", total=60, placed=datetime(2026, 3, 3))
        db.add(extra)
        show("the same view, after an add", len(new_orders), 4)
        show("the snapshot did not change", len(snapshot), 3)
        db.remove(extra)

        # `sorted` copies the built-in: a key (a field, or a tuple of fields) and `reverse`.
        # Slicing copies lists, and runs as LIMIT and OFFSET. It takes start and stop only.
        by_total = db[Order].sorted(key=Order.total, reverse=True)
        show("the three biggest orders", [o.id for o in by_total[:3]], ["o3", "o1", "o4"])
        show("page 2, two to a page", [o.id for o in by_total[2:4]], ["o4", "o2"])
        # A slice is a view too, so you can keep going: this filters the three biggest orders only.
        show("paid, among the three biggest", ids(by_total[:3].filter(Order.status == "paid")), ["o1", "o4"])

        # `len`, `sum`, `min` and `max` run inside SQLite and return one value. No record is loaded.
        # `min` and `max` copy the built-ins: an empty view needs a `default`.
        show("total of all orders", db[Order].sum(Order.total), 450)
        show("total of paid orders", db[Order].filter(Order.status == "paid").sum(Order.total), 195)
        show("total of the three biggest", by_total[:3].sum(Order.total), 395)
        show("smallest and biggest", (db[Order].min(Order.total), db[Order].max(Order.total)), (15, 200))
        show("biggest of none, with a default", db[Order].filter(Order.total > 999).max(Order.total, default=0), 0)

        # 7. Joins are plain Python.
        #
        # A set has no joins. You get whole records, and you combine them like any other values.
        # Following a relation one record at a time is cheap, because a query is a function call and not a network trip.
        section("7. Combining records")
        big = db[Order].filter(Order.total > 50).sorted(key=Order.id)
        owners = {c.id: c for c in db[Customer].filter(one_of(Customer.id, {o.customer for o in big}))}
        show(
            "who ordered what",
            [(owners[o.customer].name, o.total) for o in big],
            [("Ana", 120), ("Bob", 200), ("Cid", 75)],
        )

        # 8. Updating means storing a new version.
        #
        # `update` stores a record by key: it inserts, or replaces what is there.
        # The old value you hold does not change: nobody can change a frozen record behind your back.
        section("8. Updating")
        stale = db[Order].get("o2")
        assert stale is not None
        db.update(mark_paid(stale))
        show("the stored version", db[Order].get("o2"), mark_paid(stale))
        show("the copy you held", stale.status, "new")

        # 9. Removing records.
        #
        # The names follow `set`: `remove` needs the record to be stored and equal, `discard` does not care.
        # `delete` takes keys and `clear` takes conditions, and both are on the set itself.
        section("9. Removing")
        with refused(Missing):
            db.remove(stale)  # the stored o2 is paid by now, so it is not equal to this copy
        db.discard(stale)  # nothing happens
        show("nothing was removed", len(db[Order]), 5)
        db.remove(mark_paid(stale))  # equal to the stored one, so it goes
        db[Order].delete("o5")
        show("orders left", ids(db[Order]), ["o1", "o3", "o4"])

        # A reference can never point to nothing. This is checked at the end of the call, and the call is undone.
        with refused(BrokenRef):
            db[Order].delete("o1")  # two order lines still refer to it
        db[OrderLine].clear(OrderLine.order == "o1")
        db[Order].delete("o1")
        show("orders left", ids(db[Order]), ["o3", "o4"])

        # 10. Mistakes fail loudly.
        section("10. Safety")
        with refused(AlreadyExists):
            db.add(customers[0])
        db.update(customers[0])  # `update` replaces by key, so this is fine

        # A transaction groups calls. If anything in it fails, none of it is written.
        with refused(BrokenRef):
            with db.transaction():
                db.add(Customer(id="eve", name="Eve", city="Faro"))
                db.add(Order(id="o9", customer="nobody", status="new", total=1, placed=datetime(2026, 4, 1)))
        show("eve was not stored", db[Customer].get("eve"), None)

        # Changing a table while you loop over it is refused, like changing a dict while you loop over it.
        # SQLite does not say what such a loop would return. To change records in a loop, loop over a snapshot.
        with refused(RuntimeError):
            for order in db[Order]:
                db.update(replace(order, total=order.total + 1))
        for order in tuple(db[Order]):
            db.update(replace(order, total=order.total + 1))
        show("totals after the snapshot loop", sorted(o.total for o in db[Order]), [76, 201])

        # Python turns `and`, `or`, `not` and `1 < x < 5` into plain booleans, which would silently lose the condition.
        # So a condition refuses to be a boolean, and names the right tool.
        with refused(TypeError):
            _ = (Order.total > 10) and (Order.total < 100)
        # A misspelled field fails when the condition is built, and type checkers flag it too.
        with refused(AttributeError):
            _ = Order.totl  # type: ignore
        # A `Ref` without an index is refused when the class is defined, and the message names the fix.
        with refused(TypeError):

            @entity
            class Unindexed:
                id: str = key()
                owner: Ref[Customer]


def reopen(path: Path) -> None:
    # 11. The file is the database.
    #
    # Reopening finds the data and the same tables. There are no migrations in this version:
    # if a class no longer matches its table, `open` refuses and changes nothing.
    section("11. Reopening")
    with inlay.open(path, [Customer, Order, OrderLine]) as db:
        show("customers and orders are still there", (len(db[Customer]), len(db[Order])), (4, 2))

    @entity(table="customer")
    class RenamedField:
        id: str = key()
        full_name: str

    with refused(SchemaMismatch):
        inlay.open(path, [RenamedField])


if __name__ == "__main__":
    main()
