"""A small shop, written the way an ordinary Python app is written.

Run it from the repository root:

    uv run python examples/shop.py

The code has five parts: the data, the rules, the actions, the queries and `main`.
Comments that start with "inlay:" point at a feature of the library.
The other comments are the ones an app would have anyway.
"""

import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import inlay
from inlay import AlreadyExists, Db, RecordView, Ref, entity, key, one_of

# ---------------------------------------------------------------------------
# 1. The data
#
# inlay: a class is a frozen dataclass. `@entity` adds what the db needs, and there is no base class.
# inlay: `key()` marks the field that identifies a record. When it has no value, a unique one is made right away,
# so a record has its key before it is stored.
# inlay: `indexes` lists every index. Nothing is indexed unless it is listed here.
# ---------------------------------------------------------------------------

NEW, PAID, CANCELLED = "new", "paid", "cancelled"


@entity(indexes=[("city",)])
class Customer:
    id: str = key()  # the email address: the app can choose the key
    name: str
    city: str


@entity(indexes=[("stock",)])
class Product:
    id: str = key()
    name: str
    price: int  # in cents
    stock: int


# inlay: `Ref[Customer]` is stored as the customer's key. On a record, `order.customer` is a plain `str`.
# On the class, `Order.customer` leads to the customer's fields, so a query can use `Order.customer.city`.
# inlay: a `Ref` must be the first field of an index, so queries through it do not scan the table.
@entity(indexes=[("customer",), ("status",), ("placed",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    status: str
    placed: datetime
    total: int  # worked out when the order is placed


@entity(indexes=[("order",), ("product",)])
class OrderLine:
    id: str = key()
    order: Ref[Order]
    product: Ref[Product]
    qty: int
    unit_price: int  # copied from the product, so a later price change does not touch old orders


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
    order = Order(customer=customer_id, status=NEW, placed=now, total=total)
    # inlay: the order already has its key, so the lines can refer to it before anything is stored.
    lines = [
        OrderLine(order=order.id, product=pid, qty=qty, unit_price=products[pid].price)
        for pid, qty in basket.items()
    ]
    return order, lines


def move_to(order: Order, status: str) -> Order:
    if order.status != NEW:
        raise ShopError(f"the order is already {order.status}")
    # inlay: a record never changes. `replace` makes the next version, and the same key makes it the same record.
    return replace(order, status=status)


# ---------------------------------------------------------------------------
# 3. The actions
#
# Each action reads, applies the rules, and writes. The db is only touched here and in the queries.
# ---------------------------------------------------------------------------


def register(db: Db, email: str, name: str, city: str) -> Customer:
    customer = Customer(id=email.lower(), name=name, city=city)
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
        products = {p.id: p for p in db[Product].filter(one_of(Product.id, basket))}
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


def pay(db: Db, order_id: str) -> Order:
    order = get_order(db, order_id)
    paid = move_to(order, PAID)
    db.update(paid)
    return paid


def cancel(db: Db, order_id: str) -> Order:
    with db.transaction():
        cancelled = move_to(get_order(db, order_id), CANCELLED)
        # inlay: a view is lazy and runs its query each time it is read. `tuple` takes a snapshot, to read it twice.
        lines = tuple(db[OrderLine].filter(OrderLine.order == order_id))
        products = {p.id: p for p in db[Product].filter(one_of(Product.id, [line.product for line in lines]))}
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
# ---------------------------------------------------------------------------


def paid_orders(db: Db) -> RecordView[Order]:
    return db[Order].filter(Order.status == PAID)


def orders_in_city(orders: RecordView[Order], city: str) -> RecordView[Order]:
    # inlay: a field of a related record is reached with a dot. This is a subquery, so there is no join to write
    # and no order is returned twice.
    return orders.filter(Order.customer.city == city)


def latest_orders(db: Db, customer_id: str, page: int = 0, size: int = 5) -> RecordView[Order]:
    # `sorted` and slicing work like the built-ins, and the sorting and the slicing both happen in the db.
    newest_first = db[Order].filter(Order.customer == customer_id).sorted(key=Order.placed, reverse=True)
    return newest_first[page * size : (page + 1) * size]


def low_stock(db: Db, at_most: int) -> RecordView[Product]:
    return db[Product].filter(Product.stock <= at_most).sorted(key=Product.stock)


def waiting_too_long(db: Db, now: datetime) -> RecordView[Order]:
    # inlay: several conditions in one `filter` are combined with "and". Dates work like any other field.
    return db[Order].filter(Order.status == NEW, Order.placed < now - timedelta(days=7))


def revenue_by_customer(db: Db) -> list[tuple[Customer, int]]:
    # inlay: there is no GROUP BY. A view is iterable, and it loads one record at a time,
    # so grouping is ordinary Python over whole records.
    revenue = Counter[str]()
    for order in paid_orders(db):
        revenue[order.customer] += order.total
    # And there is no join either: look the related records up in one query and combine them.
    customers = {c.id: c for c in db[Customer].filter(one_of(Customer.id, revenue))}
    return [(customers[customer_id], total) for customer_id, total in revenue.most_common()]


# ---------------------------------------------------------------------------
# 5. The program
# ---------------------------------------------------------------------------


def money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def main() -> None:
    now = datetime(2026, 3, 1, 12, 0)

    with tempfile.TemporaryDirectory() as folder:
        # inlay: `open` creates the tables and indexes the classes need, or checks that the ones in the file match.
        # The db is closed at the end of the block, like a file.
        with inlay.open([Customer, Product, Order, OrderLine], path=Path(folder) / "shop.db") as db:
            ana = register(db, "ana@example.com", "Ana", "Lisbon")
            bob = register(db, "bob@example.com", "Bob", "Porto")
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

            pay(db, first.id)
            pay(db, second.id)
            cancel(db, mistake.id)
            try:
                pay(db, first.id)
            except ShopError as e:
                print(f"Refused: {e}")

            print("\nRevenue from paid orders")
            for customer, total in revenue_by_customer(db):
                print(f"  {customer.name:<4} {money(total):>8}")

            lisbon = orders_in_city(paid_orders(db), "Lisbon")
            # inlay: `len` and `sum` run inside the db and return one number. No record is loaded.
            print(f"\nLisbon: {len(lisbon)} paid orders, {money(lisbon.sum(Order.total))}")

            print("\nAna's latest orders")
            for order in latest_orders(db, ana.id, size=2):
                print(f"  {order.placed:%d %b %H:%M}  {order.status:<9} {money(order.total):>8}")

            print("\nLow stock")
            for product in low_stock(db, at_most=2):
                print(f"  {product.name:<8} {product.stock} left")

            print("\nWaiting for payment for over a week")
            for order in waiting_too_long(db, now):
                print(f"  {order.placed:%d %b}  {money(order.total):>8}")


if __name__ == "__main__":
    main()
