"""Optional fields, typed `X | None`, in inlay. The history experiments are paused and refuse them."""

from datetime import UTC, datetime

import pytest

import inlay
import inlay.datoms
import inlay.history
from inlay import BrokenRef, Ref, entity, is_none, key, negate, one_of


@entity
class Customer:
    id: str = key()
    city: str | None


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer, str | None]
    total: int
    discount: int | None = None
    paid: datetime | None = None


ANA = Customer(id="ana", city="Lisbon")
NOWHERE = Customer(id="nowhere", city=None)
O1 = Order(id="o1", customer="ana", total=10, discount=5, paid=datetime(2026, 11, 1, tzinfo=UTC))
O2 = Order(id="o2", customer="nowhere", total=20)
O3 = Order(id="o3", customer=None, total=30)


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db") as db:
        db.register(Customer, Order)
        db.add(ANA, NOWHERE, O1, O2, O3)
        yield db


def ids(view):
    return sorted(o.id for o in view)


def test_none_is_stored_and_read_back(db):
    assert db[Order].get("o3") == O3
    assert db[Order].get("o1") == O1
    assert db[Customer].get("nowhere") == NOWHERE


def test_a_record_with_none_can_be_removed(db):
    db.remove(O3)
    assert db[Order].get("o3") is None


def test_a_ref_to_a_missing_record_still_raises(db):
    with pytest.raises(BrokenRef):
        db.add(Order(customer="nobody", total=1))


def test_equality_matches_none_as_python_does(db):
    assert ids(db[Order].filter(is_none(Order.discount))) == ["o2", "o3"]
    assert ids(db[Order].filter(Order.discount == None)) == ["o2", "o3"]  # noqa: E711
    assert ids(db[Order].filter(Order.discount != 5)) == ["o2", "o3"]
    assert ids(db[Order].filter(negate(is_none(Order.discount)))) == ["o1"]


def test_an_ordering_comparison_is_false_for_none_and_its_negation_true(db):
    assert ids(db[Order].filter(Order.discount > 1)) == ["o1"]  # pyright: ignore[reportOptionalOperand]
    assert ids(db[Order].filter(negate(Order.discount > 1))) == ["o2", "o3"]  # pyright: ignore[reportOptionalOperand]


def test_one_of_can_hold_none(db):
    assert ids(db[Order].filter(one_of(Order.discount, (None, 5)))) == ["o1", "o2", "o3"]


def test_a_path_through_an_empty_ref_matches_nothing_and_its_negation_everything(db):
    assert ids(db[Order].filter(Order.customer.city == "Lisbon")) == ["o1"]
    assert ids(db[Order].filter(negate(Order.customer.city == "Lisbon"))) == ["o2", "o3"]
    assert ids(db[Order].filter(is_none(Order.customer.city))) == ["o2"]
    assert ids(db[Order].filter(is_none(Order.customer))) == ["o3"]


def test_none_sorts_first_and_last_in_reverse(db):
    assert [o.id for o in db[Order].sorted(key=(Order.discount, Order.total))] == ["o2", "o3", "o1"]
    assert [o.id for o in db[Order].sorted(key=(Order.discount, Order.total), reverse=True)] == ["o1", "o3", "o2"]


def test_aggregates_skip_none_and_group_it_under_none(db):
    orders = db[Order]
    assert (len(orders), orders.sum(Order.discount), orders.min(Order.discount)) == (3, 5, 5)
    assert orders.aggregate_by(Order.discount, len) == {None: 2, 5: 1}


def test_values_that_are_all_none_sum_to_0_and_have_no_min(db):
    assert db[Order].aggregate_by(Order.customer, sum, Order.discount) == {None: 0, "ana": 5, "nowhere": 0}
    assert db[Order].aggregate_by(Order.customer, max, Order.discount) == {None: None, "ana": 5, "nowhere": None}
    assert db[Order].filter(Order.customer == "nowhere").max(Order.discount) is None


def test_the_key_cannot_be_optional():
    with pytest.raises(TypeError, match="is the key and must be a str"):

        @entity
        class Thing:
            id: str | None = key()


def test_other_unions_are_refused():
    with pytest.raises(TypeError, match="unsupported type"):

        @entity
        class Thing:
            id: str = key()
            amount: int | str

    with pytest.raises(TypeError, match="unsupported type"):

        @entity
        class Other:
            id: str = key()
            customer: Ref[Customer] | None


def test_the_paused_history_experiments_refuse_optional_fields(tmp_path):
    with inlay.history.open(tmp_path / "history.db") as db, pytest.raises(TypeError, match="Order.customer"):
        db.register(Customer, Order)
    with pytest.raises(TypeError, match="Customer.city"):
        inlay.datoms.open([Customer, Order], path=tmp_path / "datoms.db")
