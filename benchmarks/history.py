"""inlay and its two history experiments side by side: the same writes and reads, and the reads at past transactions.

A third column runs the history db with its declared indexes the other way round, field first and `_tx_to` second,
to compare the two orders without changing the code. The fourth column is the datoms version,
and the last two give the history db's and the datoms version's times as multiples of inlay's.

Run from the repository root:

    uv run python benchmarks/history.py
    uv run python benchmarks/history.py --orders 100000 --versions 20

Times are in milliseconds. Reads are the best of three runs; writes change the data, so they run once.
"""

import argparse
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import inlay
import inlay.datoms
import inlay.history
from inlay import Ref, entity, key

CUSTOMERS = 2_000
GETS = 2_000
SINGLE_UPDATES = 1_000
REMOVES = 1_000
MIXED_CUSTOMERS = 200
MIXED_ORDERS = 2_000


@entity
class Customer:
    id: str = key()
    name: str


@entity(indexes=[("customer",), ("total",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@dataclass(frozen=True)
class Workload:
    customers: list[Customer]
    orders: list[Order]
    rounds: list[list[Order]]  # every order, updated, once per round
    singles: list[Order]  # updated one call at a time
    removed: list[Order]
    renamed: list[Customer]  # every customer, with a new name
    mixed_orders: list[Order]  # updated in one call with customers that get their names back
    extra_round: list[Order]  # one more round, run after the reads
    gets: list[str]  # keys of orders that exist at every transaction


def workload(orders: int, versions: int) -> Workload:
    random.seed(1)
    customers = [Customer(name=f"c{i}") for i in range(CUSTOMERS)]
    first = [Order(customer=random.choice(customers).id, total=random.randrange(1000)) for _ in range(orders)]
    rounds = [[replace(o, total=random.randrange(1000)) for o in first] for _ in range(versions)]
    singles = [replace(o, total=random.randrange(1000)) for o in random.sample(rounds[-1], SINGLE_UPDATES)]
    current = {o.id: o for o in rounds[-1]} | {o.id: o for o in singles}
    removed = random.sample(list(current.values()), REMOVES)
    removed_keys = {o.id for o in removed}
    remaining = [o for o in current.values() if o.id not in removed_keys]
    renamed = [replace(c, name=c.name + "'") for c in customers]
    mixed_orders = [replace(o, total=random.randrange(1000)) for o in random.sample(remaining, MIXED_ORDERS)]
    extra_round = [replace(o, total=random.randrange(1000)) for o in remaining]
    gets = [o.id for o in random.sample(remaining, GETS)]
    return Workload(customers, first, rounds, singles, removed, renamed, mixed_orders, extra_round, gets)


def once(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


def best(fn: Callable[[], object]) -> float:
    return min(once(fn) for _ in range(3))


type AnyDb = inlay.Db | inlay.history.Db | inlay.datoms.Db
type HistoryDb = inlay.history.Db | inlay.datoms.Db


def open_shop(open_db: Callable[..., Any], path: Path) -> Any:
    """The db at `path` with `Customer` and `Order`; the datoms version, which is paused, still takes them in `open`."""
    if open_db is inlay.datoms.open:
        return open_db([Customer, Order], path=path)
    db = open_db(path)
    if Customer not in db:
        db.register(Customer, Order)
    return db


def write_all(db: AnyDb, w: Workload) -> dict[str, float]:
    return {
        "add everything": once(lambda: db.add(*w.customers, *w.orders)),
        f"update all orders, mean of {len(w.rounds)} rounds": sum(once(lambda r=r: db.update(*r)) for r in w.rounds)
        / len(w.rounds),
        f"{SINGLE_UPDATES} updates of one order": once(lambda: [db.update(o) for o in w.singles]),
        f"remove {REMOVES} orders": once(lambda: db.remove(*w.removed)),
        f"update all {CUSTOMERS} customers": once(lambda: db.update(*w.renamed)),
        f"{MIXED_CUSTOMERS} customers and {MIXED_ORDERS} orders in one call": once(
            lambda: db.update(*w.customers[:MIXED_CUSTOMERS], *w.mixed_orders)
        ),
    }


type Orders = Callable[
    [], inlay.Query[Order] | inlay.history.RecordView[Order] | inlay.datoms.RecordView[Order]
]


def reads(orders: Orders, w: Workload, when: str) -> dict[str, float]:
    customer = w.customers[7]
    return {
        f"{when}: load all orders": best(lambda: list(orders())),
        f"{when}: filter customer == one": best(lambda: list(orders().filter(Order.customer == customer.id))),
        f"{when}: filter total > 989 (about 1%)": best(lambda: list(orders().filter(Order.total > 989))),
        f"{when}: filter through the Ref": best(lambda: list(orders().filter(Order.customer.name == customer.name))),
        f"{when}: sorted by total, first 10": best(lambda: list(orders().sorted(key=Order.total)[:10])),
        f"{when}: {GETS} gets": best(lambda: [orders().get(k) for k in w.gets]),
        f"{when}: len and sum": best(lambda: (len(orders()), orders().sum(Order.total))),
    }


def history_reads(open_db: Callable[..., HistoryDb], path: Path, w: Workload) -> dict[str, float]:
    """Reads now and at three past transactions, and `as_of` with the time of each."""
    with open_shop(open_db, path) as db:
        results = reads(lambda: db[Order], w, "now")
        # The history db registers its classes in a transaction of their own, before the first write.
        first = 2 if open_db is inlay.history.open else 1
        points = {"the first": first, "a middle": first + len(w.rounds) // 2, "the latest": db.tx}
        with closing(sqlite3.connect(path)) as conn:
            at = 'SELECT "at" FROM "_inlay_tx" WHERE "tx" = ?'
            times = {name: conn.execute(at, (tx,)).fetchone()[0] for name, tx in points.items()}
        for name, tx in points.items():
            snapshot = db.as_of(tx)
            # The datoms version fills a table for the snapshot on its first read, which the best of three would hide.
            results[f"at {name} transaction: first read, len"] = once(lambda s=snapshot: len(s[Order]))
            results |= reads(lambda s=snapshot: s[Order], w, f"at {name} transaction")
        for name, at in times.items():
            when = datetime.fromtimestamp(at / 1000, UTC)
            results[f"as_of the time of {name} transaction"] = best(lambda when=when: db.as_of(when))
    return results


def one_more_round(open_db: Callable[..., AnyDb], path: Path, w: Workload) -> dict[str, float]:
    with open_shop(open_db, path) as db:
        return {"one more update round, after the history": once(lambda: db.update(*w.extra_round))}


def put_fields_first(path: Path) -> None:
    """Rebuild the history db's declared indexes of `Order` as (field, _tx_to). The db keeps indexes that it finds by name."""
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        for field in ("customer", "total"):
            conn.execute(f'DROP INDEX "order__{field}"')
            conn.execute(f'CREATE INDEX "order__{field}" ON "order" ("{field}", "_tx_to")')


def size_mb(path: Path) -> float:
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*")) / 1e6


def main() -> None:
    parser = argparse.ArgumentParser(description="inlay and its history experiments side by side.")
    parser.add_argument("--orders", type=int, default=20_000)
    parser.add_argument("--versions", type=int, default=10, help="rounds of updating every order")
    args = parser.parse_args()
    w = workload(args.orders, args.versions)

    with tempfile.TemporaryDirectory() as folder:
        plain, history, flipped, datoms = (
            Path(folder) / name for name in ("inlay.db", "history.db", "flipped.db", "datoms.db")
        )
        runs: dict[str, tuple[Callable[..., Any], Path]] = {
            "inlay": (inlay.open, plain),
            "history": (inlay.history.open, history),
            "history, field first": (inlay.history.open, flipped),
            "datoms": (inlay.datoms.open, datoms),
        }
        columns: dict[str, dict[str, float]] = {name: {} for name in runs}
        for name in ("inlay", "history", "datoms"):  # the field-first column is a copy of the history file
            open_db, path = runs[name]
            with open_shop(open_db, path) as db:
                columns[name] |= write_all(db, w)
                if name == "inlay":  # the history experiments read now in `history_reads`
                    columns[name] |= reads(lambda: db[Order], w, "now")
        shutil.copy(history, flipped)  # closing the db wrote everything into the main file
        put_fields_first(flipped)
        for name, (open_db, path) in runs.items():
            if name != "inlay":
                columns[name] |= history_reads(open_db, path, w)
            columns[name] |= one_more_round(open_db, path, w)
        sizes = {name: size_mb(path) for name, (_, path) in runs.items()}

    print(f"{args.orders} orders of {CUSTOMERS} customers, every order updated {args.versions} times")
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}; times in ms\n")
    labels = list(dict.fromkeys(label for results in columns.values() for label in results))
    width = max(map(len, labels)) + 2
    ratios = {"history / inlay": "history", "datoms / inlay": "datoms"}
    print(" " * width + "".join(f"{name:>22}" for name in columns) + "".join(f"{r:>17}" for r in ratios))
    for label in labels:
        values = [results.get(label) for results in columns.values()]
        cells = "".join(f"{v:>22.2f}" if v is not None else f"{'':>22}" for v in values)
        base = columns["inlay"].get(label)
        for name in ratios.values():
            v = columns[name].get(label)
            cells += f"{v / base:>16.1f}x" if base and v else f"{'':>17}"
        print(f"{label:{width}}{cells}")
    print(f"\n{'file size, MB':{width}}" + "".join(f"{size:>22.1f}" for size in sizes.values()))


if __name__ == "__main__":
    main()
