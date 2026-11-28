"""A small shop, written the way an ordinary Python app is written.

Run it from the repository root:

    uv run python examples/shop.py

The code has five parts: the data, the rules, the actions, the queries and `main`.
Comments that start with "inlay:" point at a feature of the library.
The other comments are the ones an app would have anyway.
"""

import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import inlay
from inlay import AlreadyExists, Db, Field, Function, Query, Ref, coalesce, default, entity, key, projection, query, to_month

# ---------------------------------------------------------------------------
# 1. The data
#
# inlay: a class is a frozen dataclass. `@entity` adds what the db needs, and there is no base class.
# inlay: each field is declared `Field[...]`: on a record it is the value, and on the class a value for queries,
# so type checkers check both. A default is written `default(...)`.
# inlay: `key()` marks the field that identifies a record. When it has no value, a unique one is made right away,
# so a record has its key before it is stored.
# inlay: `indexes` lists every index. Nothing is indexed unless it is listed here.
# ---------------------------------------------------------------------------

# inlay: a `StrEnum` or an `IntEnum` field is stored as its value and read back as a member.
class Status(StrEnum):
    NEW = "new"
    PAID = "paid"
    CANCELLED = "cancelled"


# inlay: a field typed `X | None` is optional, and an optional reference is written `Ref[Customer, str | None]`.
# A class can refer to itself.
@entity(indexes=[("city",), ("referred_by",)])
class Customer:
    id: Field[str] = key()  # the email address: the app can choose the key
    name: Field[str]
    city: Field[str]
    referred_by: Ref[Customer, str | None] = default(None)


@entity(indexes=[("stock",)])
class Product:
    id: Field[str] = key()
    name: Field[str]
    price: Field[int]  # in cents
    stock: Field[int]


# inlay: `Ref[Customer]` is stored as the customer's key. On a record, `order.customer` is a plain `str`.
# On the class, `Order.customer` leads to the customer's fields, so a query can use `Order.customer.city`,
# and compares by the key, `Order.customer.id == ...`, which reads the reference itself.
# inlay: a `Ref` must be the first field of an index, so queries through it do not scan the table.
@entity(indexes=[("customer",), ("status",), ("placed",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    status: Field[Status]
    placed: Field[datetime]
    total: Field[int]  # worked out when the order is placed
    paid: Field[datetime | None] = default(None)


@entity(indexes=[("order",), ("product",)])
class OrderLine:
    id: Field[str] = key()
    order: Ref[Order]
    product: Ref[Product]
    qty: Field[int]
    unit_price: Field[int]  # copied from the product, so a later price change does not touch old orders


# ---------------------------------------------------------------------------
# 2. The rules
#
# inlay: records are plain values, so the rules are plain functions from values to values.
# They do not know about the db, and they can be tested without one.
# ---------------------------------------------------------------------------


class ShopError(Exception):
    pass


def build_order(
    customer_id: str, basket: Mapping[str, int], products: Mapping[str, Product], now: datetime
) -> tuple[Order, list[OrderLine]]:
    total = sum(products[pid].price * qty for pid, qty in basket.items())
    order = Order(customer=customer_id, status=Status.NEW, placed=now, total=total)
    # inlay: the order already has its key, so the lines can refer to it before anything is stored.
    lines = [
        OrderLine(order=order.id, product=pid, qty=qty, unit_price=products[pid].price)
        for pid, qty in basket.items()
    ]
    return order, lines


def move_to(order: Order, status: Status) -> Order:
    if order.status != Status.NEW:
        raise ShopError(f"the order is already {order.status}")
    # inlay: a record never changes. `replace` makes the next version, and the same key makes it the same record.
    return replace(order, status=status)


# ---------------------------------------------------------------------------
# 3. The actions
#
# Each action reads, applies the rules, and writes. The db is only touched here and in the queries.
# ---------------------------------------------------------------------------


def register(db: Db, email: str, name: str, city: str, referred_by: str | None = None) -> Customer:
    customer = Customer(id=email.lower(), name=name, city=city, referred_by=referred_by)
    try:
        # inlay: `add` refuses a key that exists, and `update` would replace it.
        # The names are the ones `set` and `dict` use.
        db.add(customer)
    except AlreadyExists:
        raise ShopError(f"{email} is already registered") from None
    return customer


def place_order(db: Db, customer_id: str, basket: Mapping[str, int], now: datetime) -> Order:
    # The clock is a parameter, so the action does the same thing every time it is given the same input.
    #
    # inlay: a transaction takes the write lock when it starts. No other writer can sell this stock
    # between the check below and the update at the end. If anything raises, nothing is written.
    with db.transaction():
        if db[Customer].get(customer_id) is None:
            raise ShopError(f"unknown customer {customer_id}")
        # inlay: `one_of` is an `IN` test, and a dict gives its keys. The result is whole `Product` records.
        products = {p.id: p for p in db[Product].filter(Product.id.one_of(basket))}
        if unknown := basket.keys() - products.keys():
            raise ShopError(f"unknown products {sorted(unknown)}")
        if short := [products[pid].name for pid, qty in basket.items() if products[pid].stock < qty]:
            raise ShopError(f"not enough stock of {', '.join(short)}")

        order, lines = build_order(customer_id, basket, products, now)
        # inlay: one call can mix classes, and the order inside it does not matter.
        # References are checked when the transaction ends, so a customer that did not exist would be refused then.
        db.add(order, *lines)
        db.update(*(replace(products[pid], stock=products[pid].stock - qty) for pid, qty in basket.items()))
    return order


def pay(db: Db, order_id: str, now: datetime) -> Order:
    order = get_order(db, order_id)
    paid = replace(move_to(order, Status.PAID), paid=now)
    db.update(paid)
    return paid


def cancel(db: Db, order_id: str) -> Order:
    with db.transaction():
        cancelled = move_to(get_order(db, order_id), Status.CANCELLED)
        # inlay: a view is lazy and runs its query each time it is read. `tuple` takes a snapshot, to read it twice.
        lines = tuple(db[OrderLine].filter(OrderLine.order.id == order_id))
        products = {p.id: p for p in db[Product].filter(Product.id.one_of([line.product for line in lines]))}
        restocked = (replace(products[line.product], stock=products[line.product].stock + line.qty) for line in lines)
        db.update(cancelled, *restocked)
    return cancelled


def get_order(db: Db, order_id: str) -> Order:
    order = db[Order].get(order_id)
    if order is None:
        raise ShopError(f"unknown order {order_id}")
    return order


# ---------------------------------------------------------------------------
# 4. The queries
#
# inlay: `db[Order]` is a set of orders. `filter`, `sorted` and slicing return new views, and nothing runs until
# a view is read. A view is a value, so functions can take one, add to it, and give it back.
# inlay: a query needs no db to be built. `query(Order)` starts one, and `db[q]` runs it, so a query used in
# many places can be defined once, here.
# ---------------------------------------------------------------------------

paid_orders = query(Order).filter(Order.status == Status.PAID)


def orders_in_city(orders: Query[Order], city: str) -> Query[Order]:
    # inlay: a field of a related record is reached with a dot. This is a subquery, so there is no join to write
    # and no order is returned twice.
    return orders.filter(Order.customer.city == city)


def latest_orders(db: Db, customer_id: str, page: int = 0, size: int = 5) -> Query[Order]:
    # `sorted` and slicing work like the built-ins, and the sorting and the slicing both happen in the db.
    newest_first = db[Order].filter(Order.customer.id == customer_id).sorted(key=Order.placed, reverse=True)
    return newest_first[page * size : (page + 1) * size]


def low_stock(db: Db, at_most: int) -> Query[Product]:
    return db[Product].filter(Product.stock <= at_most).sorted(key=Product.stock)


def waiting_too_long(db: Db, now: datetime) -> Query[Order]:
    # inlay: dates work like any other field.
    return db[Order].filter((Order.status == Status.NEW) & (Order.placed < now - timedelta(days=7)))


def find_customers(db: Db, text: str) -> Query[Customer]:
    # inlay: a text field has `startswith`, `endswith` and `contains`, and any field `one_of`.
    return db[Customer].filter(Customer.name.contains(text)).sorted(key=Customer.name)


def orders_per_month(db: Db) -> Mapping[date, int]:
    # inlay: functions such as `to_month` work wherever a field does, and `len` counts.
    # inlay: `~` negates a condition, and `&` and `|` combine them.
    return db[Order].filter(~(Order.status == Status.CANCELLED)).aggregate_by(to_month(Order.placed), len)


# inlay: a function of your own is written twice, in Python for plain values and in SQL for the db.
hour = Function("hour", (datetime,), int, lambda x: x.hour, "CAST(strftime('%H', {}) AS INTEGER)")


def orders_per_hour(db: Db) -> Mapping[int, int]:
    return db[Order].aggregate_by(hour(Order.placed), len)


def revenue_from_referrals(db: Db, customer_id: str) -> int:
    # inlay: a path can go through an optional reference. A customer that nobody referred matches nothing.
    return db[paid_orders].filter(Order.customer.referred_by.id == customer_id).sum(Order.total)


# inlay: a projection is the shape of a result, such as the rows an API returns: fields of the order and values
# of its customer, read in one query. It has no table, and a view turns into it with `project`.
@projection(Order)
class OrderRow:
    id: Field[str]
    placed: Field[datetime]
    total: Field[int]
    customer_name: Field[str] = Order.customer.name
    city: Field[str] = Order.customer.city


# inlay: the other way along a `Ref`, from a customer to its orders, reaches many records, so a projection takes it
# through an aggregate: over the orders that point at each customer, along `Order.customer`, the only `Ref` there.
# `filter=` keeps only some of them, and a `Ref` to the class itself is named. Over no orders a sum is 0.
# inlay: `coalesce` is the first value that is not None, here the referrer's name or else "nobody".
@projection(Customer)
class CustomerRow:
    id: Field[str]
    name: Field[str]
    referrer: Field[str] = coalesce(Customer.referred_by.name, "nobody")
    orders: Field[int] = Order.id.count()
    spent: Field[int] = Order.total.sum(filter=Order.status == Status.PAID)
    items: Field[int] = OrderLine.qty.sum(OrderLine.order).sum()
    referrals: Field[int] = Customer.id.count(Customer.referred_by)


def top_customers(db: Db, n: int) -> Query[CustomerRow]:
    # inlay: the aggregates are fields like any other, so the db sorts and slices by them.
    return db[Customer].project(CustomerRow).sorted(key=CustomerRow.spent, reverse=True)[:n]


def latest_paid(db: Db, city: str | None = None) -> Query[OrderRow]:
    # inlay: conditions on the order come before `project`, and the fields of the projection work after it.
    rows = db[paid_orders].project(OrderRow)
    if city is not None:
        rows = rows.filter(OrderRow.city == city)
    return rows.sorted(key=OrderRow.placed, reverse=True)[:3]


def stock_value(db: Db) -> int:
    # inlay: arithmetic on fields is computed by the db.
    return db[Product].sum(Product.price * Product.stock)


# ---------------------------------------------------------------------------
# 5. The program
# ---------------------------------------------------------------------------


def money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def main() -> None:
    now = datetime(2026, 3, 1, 12, 0)

    with tempfile.TemporaryDirectory() as folder:
        # inlay: `open` opens the file, and `register` creates the tables and indexes of the classes in it,
        # each after the classes it refers to, or with them. The db is closed at the end of the block, like a file.
        # inlay: `register` raises if a table exists, so an app checks with `in` first. Inside a transaction,
        # several processes starting on a new file at once do not race to create the tables.
        with inlay.open(Path(folder) / "shop.db") as db:
            with db.transaction():
                if Customer not in db:
                    db.register(Customer, Product, Order, OrderLine)
            ana = register(db, "ana@example.com", "Ana", "Lisbon")
            bob = register(db, "bob@example.com", "Bob", "Porto", referred_by=ana.id)
            cid = register(db, "cid@example.com", "Cid", "Lisbon")
            db.add(
                Product(id="keyboard", name="Keyboard", price=5000, stock=10),
                Product(id="mouse", name="Mouse", price=2000, stock=10),
                Product(id="monitor", name="Monitor", price=20000, stock=3),
                Product(id="cable", name="Cable", price=500, stock=4),
            )

            try:
                register(db, "ANA@example.com", "Ana again", "Faro")
            except ShopError as e:
                print(f"Refused: {e}")

            first = place_order(db, ana.id, {"keyboard": 1, "mouse": 2}, now)
            second = place_order(db, bob.id, {"monitor": 1}, now + timedelta(hours=1))
            place_order(db, cid.id, {"cable": 3}, now - timedelta(days=10))
            mistake = place_order(db, ana.id, {"monitor": 1}, now + timedelta(hours=2))
            place_order(db, ana.id, {"mouse": 1}, now + timedelta(hours=3))

            try:
                place_order(db, bob.id, {"cable": 2}, now + timedelta(hours=4))
            except ShopError as e:
                print(f"Refused: {e}")

            pay(db, first.id, now + timedelta(hours=5))
            pay(db, second.id, now + timedelta(hours=6))
            cancel(db, mistake.id)
            try:
                pay(db, first.id, now + timedelta(hours=7))
            except ShopError as e:
                print(f"Refused: {e}")

            print("\nTop customers")
            for row in top_customers(db, 2):
                print(f"  {row.name:<4} {money(row.spent):>8}  {row.orders} orders, {row.items} items, "
                      f"referred by {row.referrer}, referred {row.referrals}")

            print("\nCustomers named like 'a'")
            for customer in find_customers(db, "a"):
                print(f"  {customer.name}")

            lisbon = orders_in_city(db[paid_orders], "Lisbon")
            # inlay: `len` and `sum` run inside the db and return one number. No record is loaded.
            print(f"\nLisbon: {len(lisbon)} paid orders, {money(lisbon.sum(Order.total))}")
            print(f"Brought in by Ana's referrals: {money(revenue_from_referrals(db, ana.id))}")

            print("\nOrders per month")
            for month, count in orders_per_month(db).items():
                print(f"  {month:%b %Y}  {count}")

            print("\nOrders per hour of the day")
            for at, count in orders_per_hour(db).items():
                print(f"  {at:02}:00  {count}")

            print("\nAna's latest orders")
            for order in latest_orders(db, ana.id, size=3):
                paid = "not paid" if order.paid is None else f"paid {order.paid:%H:%M}"
                print(f"  {order.placed:%d %b %H:%M}  {order.status:<9} {money(order.total):>8}  {paid}")

            print("\nLatest paid orders in Porto")
            for row in latest_paid(db, city="Porto"):
                print(f"  {row.placed:%d %b}  {row.customer_name:<4} {money(row.total):>8}")

            print(f"\nStock worth {money(stock_value(db))}")
            print("\nLow stock")
            for product in low_stock(db, at_most=2):
                print(f"  {product.name:<8} {product.stock} left")

            print("\nWaiting for payment for over a week")
            for order in waiting_too_long(db, now):
                print(f"  {order.placed:%d %b}  {money(order.total):>8}")


if __name__ == "__main__":
    main()
