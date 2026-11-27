"""What aggregate_by gives: a mapping read from the db, which queries can also look up by the fields of their records."""

import pytest

import inlay
from inlay import Ref, entity, key


@entity
class Customer:
    id: str = key()
    city: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@pytest.fixture
def db(tmp_path):
    with inlay.open(tmp_path / "shop.db") as db:
        db.register(Customer, Order)
        db.add(Customer(id="ana", city="Lisbon"), Customer(id="bob", city="Porto"))
        db.add(Order(id="o1", customer="ana", total=150), Order(id="o2", customer="ana", total=50))
        yield db


def test_every_read_runs_its_query_again(db):
    totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
    assert dict(totals) == {"ana": 200}
    db.add(Order(id="o3", customer="bob", total=30))
    assert (totals["bob"], len(totals), "bob" in totals) == (30, 2, True)


def ids(view):
    return sorted(r.id for r in view)


def test_a_query_reads_the_value_of_each_records_own_group(db):
    biggest = db[Order].aggregate_by(Order.customer, max, Order.total)
    biggest_orders = db[Order].filter(Order.total == biggest[Order.customer])
    assert ids(biggest_orders) == ["o1"]
    db.add(Order(id="o3", customer="bob", total=30))
    assert ids(biggest_orders) == ["o1", "o3"]


def test_another_class_looks_it_up_by_its_key_and_gets_none_for_a_key_with_no_records(db):
    totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
    assert ids(db[Customer].filter(totals[Customer.id] > 100)) == ["ana"]
    assert ids(db[Customer].filter(totals[Customer.id] == None)) == ["bob"]


def test_keys_read_through_a_ref_and_several_keys(db):
    per_city = db[Order].aggregate_by(Order.customer.city, sum, Order.total)
    assert ids(db[Order].filter(per_city[Order.customer.city] == 200)) == ["o1", "o2"]
    pairs = db[Order].aggregate_by((Order.customer, Order.total), len)
    assert ids(db[Order].filter(pairs[Order.customer, Order.total] == 1)) == ["o1", "o2"]


def test_it_is_looked_up_with_values_like_its_keys(db):
    totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
    with pytest.raises(TypeError, match="Order.total is a int, but the keys are a str"):
        totals[Order.total]
    with pytest.raises(TypeError, match="the keys are 2 values, got 1"):
        db[Order].aggregate_by((Order.customer, Order.total), len)[Order.customer]
    with pytest.raises(ValueError, match="Order.customer cannot be used on Customer"):
        db[Customer].filter(totals[Order.customer] > 100)


def test_writing_the_grouped_class_while_looping_is_refused(db):
    totals = db[Order].aggregate_by(Order.customer, sum, Order.total)
    with pytest.raises(RuntimeError, match="Order changed during iteration"):
        for _ in db[Customer].filter(totals[Customer.id] > 100):
            db.add(Order(id="o3", customer="bob", total=30))


@pytest.fixture
def totals(db):
    db.add(Order(id="o3", customer="bob", total=30))
    return db[Order].aggregate_by(Order.customer, sum, Order.total)


def test_groups_are_filtered_by_their_value_and_by_their_keys(totals):
    assert dict(totals.filter(totals.value > 100)) == {"ana": 200}
    assert dict(totals.filter(Order.customer != "ana")) == {"bob": 30}


def test_groups_are_sorted_and_sliced_and_their_lookups_follow(db, totals):
    assert list(totals.sorted(key=totals.value)) == ["bob", "ana"]
    top = totals.sorted(key=totals.value, reverse=True)[:1]
    assert dict(top) == {"ana": 200}
    with pytest.raises(KeyError):
        top["bob"]
    assert ids(db[Customer].filter(top[Customer.id] == None)) == ["bob"]


def test_a_group_is_looked_up_by_all_its_keys(db):
    pairs = db[Order].aggregate_by((Order.customer, Order.total), len)
    assert (pairs["ana", 150], ("ana", 1) in pairs) == (1, False)


def test_a_condition_on_a_field_that_is_not_a_key_is_refused(totals):
    with pytest.raises(ValueError, match="Order.total is not a key of these groups"):
        totals.filter(Order.total > 100)
