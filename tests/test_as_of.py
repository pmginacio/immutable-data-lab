import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from helpers import opened

import inlay.datoms
import inlay.history
from inlay import Field, Ref, entity, key
from inlay.history.sql import tx_time


@entity
class Customer:
    id: Field[str] = key()
    name: Field[str]


@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]


ANA = Customer(id="ana", name="Ana")
O1 = Order(id="o1", customer="ana", total=10)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shop.db"


# Both history experiments number transactions and read past ones the same way.
@pytest.fixture(params=[inlay.history.open, inlay.datoms.open], ids=["history", "datoms"])
def db(request, path):
    with opened(request.param, [Customer, Order], path, [ANA, O1]) as db:
        yield db


def transactions(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute('SELECT "tx", "at" FROM "_inlay_tx" ORDER BY "tx"').fetchall()


def test_a_removed_key_can_be_added_again(db):
    db.remove(O1)
    db.add(replace(O1, total=30))
    assert [o.total for o in db[Order]] == [30]
    assert list(db.as_of(2)[Order]) == []


def test_each_write_takes_the_next_transaction_and_times_never_go_back(db, path):
    future = tx_time(datetime(2999, 1, 1, tzinfo=UTC))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('UPDATE "_inlay_tx" SET "at" = ?', (future,))
    db.update(replace(O1, total=20))
    assert transactions(path) == [(1, future), (2, future)]


def test_a_snapshot_reads_the_records_as_they_were(db):
    db.update(replace(O1, total=20))
    db.add(Order(id="o2", customer="ana", total=5))
    db[Order].delete("o1")
    assert [o.total for o in db.as_of(1)[Order]] == [10]
    assert [o.total for o in db.as_of(2)[Order]] == [20]
    assert [o.id for o in db.as_of(4)[Order]] == ["o2"]
    assert list(db.as_of(0)[Order]) == []


def test_a_snapshot_does_not_change_when_the_db_does(db):
    now = db.as_of(db.tx)
    db.update(replace(O1, total=20))
    assert [o.total for o in now[Order]] == [10]
    assert [o.total for o in db[Order]] == [20]


def test_a_snapshot_reads_related_records_at_the_same_transaction(db):
    db.update(replace(ANA, name="Ana B"))
    assert [o.id for o in db.as_of(1)[Order].filter(Order.customer.name == "Ana")] == ["o1"]
    assert list(db.as_of(1)[Order].filter(Order.customer.name == "Ana B")) == []


def test_a_snapshot_aggregates_and_slices_at_its_transaction(db):
    db.add(Order(id="o2", customer="ana", total=5))
    db.update(replace(O1, total=20), replace(ANA, name="Ana B"))
    then = db.as_of(2)[Order]
    assert (len(then), then.sum(Order.total)) == (2, 15)
    assert then.aggregate_by(Order.customer, sum, Order.total) == {"ana": 15}
    # A filter after a slice runs on a SELECT of the slice, and its relation must still read the customer at 2.
    assert [o.id for o in then.sorted(key=Order.total)[:1].filter(Order.customer.name == "Ana")] == ["o2"]


def test_as_of_a_time_reads_the_last_transaction_at_or_before_it(db, path):
    db.update(replace(O1, total=20))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            'UPDATE "_inlay_tx" SET "at" = ? WHERE "tx" = ?',
            [(tx_time(datetime(2026, 1, 1, tzinfo=UTC)), 1), (tx_time(datetime(2026, 2, 1, tzinfo=UTC)), 2)],
        )
    assert [o.total for o in db.as_of(datetime(2026, 1, 15, tzinfo=UTC))[Order]] == [10]
    assert [o.total for o in db.as_of(datetime(2026, 2, 1, tzinfo=UTC))[Order]] == [20]
    # The same moment as 2026-02-01 00:00 UTC, two hours ahead.
    assert [o.total for o in db.as_of(datetime(2026, 2, 1, 2, tzinfo=timezone(timedelta(hours=2))))[Order]] == [20]
    assert list(db.as_of(datetime(2025, 1, 1, tzinfo=UTC))[Order]) == []


def test_a_transaction_is_recorded_at_the_time_it_commits(db, path):
    before = tx_time(datetime.now(UTC))
    db.update(replace(O1, total=20))
    after = tx_time(datetime.now(UTC))
    tx, at = transactions(path)[-1]
    assert tx == 2 and before <= at <= after
    assert db.as_of(datetime.now(UTC)).tx == db.tx == 2


@pytest.mark.parametrize(
    "at, message", [(datetime(2026, 1, 1), "timezone"), (2, "no committed transaction 2"), (-1, "no committed transaction -1")]
)
def test_as_of_refuses_a_naive_time_and_a_transaction_that_does_not_exist(db, at, message):
    with pytest.raises(ValueError, match=message):
        db.as_of(at)


def test_a_snapshot_inside_a_transaction_reads_the_last_committed_one(db):
    with db.transaction():
        db.update(replace(O1, total=20))
        before = db.as_of(db.tx)
        now = db.as_of(datetime.now(UTC))
        db.update(replace(O1, total=30))
        with pytest.raises(ValueError, match="no committed transaction 2"):
            db.as_of(2)
    assert [o.total for o in before[Order]] == [o.total for o in now[Order]] == [10]


def test_a_snapshot_first_read_in_a_transaction_that_rolls_back_still_reads_its_records(db):
    with pytest.raises(RuntimeError), db.transaction():
        snapshot = db.as_of(1)
        assert list(snapshot[Order]) == [O1]
        raise RuntimeError
    assert list(snapshot[Order]) == [O1]


def test_writing_while_looping_over_a_snapshot_of_the_same_class_is_refused(db):
    with pytest.raises(RuntimeError, match="Order changed during iteration"):
        for _ in db.as_of(1)[Order]:
            db.update(replace(O1, total=20))
