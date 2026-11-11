import sqlite3

import pytest

from inlay import Ref, entity, key
from inlay.entity import meta
from inlay.sql import Query, aggregate_sql, create_table_sql, insert_sql, select_sql


@entity(indexes=[("city",)])
class Customer:
    id: str = key()
    name: str
    city: str
    age: int


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


RECORDS = [
    Customer(id="c1", name="Dora", city="Faro", age=40),
    Customer(id="c2", name="Ana", city="Lisbon", age=30),
    Customer(id="c3", name="Bob", city="Lisbon", age=20),
    Customer(id="c4", name="Cid", city="Porto", age=50),
    Customer(id="c5", name="Eva", city="Lisbon", age=30),
]
BY_NAME = ["c2", "c3", "c4", "c1", "c5"]


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    m = meta(Customer)
    conn.execute(create_table_sql(m))
    for c in RECORDS:
        conn.execute(insert_sql(m), [getattr(c, f.name) for f in m.fields])
    yield conn
    conn.close()


def ids(conn, query):
    sql, params = select_sql(meta(Customer), query)
    return [row[0] for row in conn.execute(sql, params)]


def aggregate(conn, query, function, field=None):
    sql, params = aggregate_sql(meta(Customer), query, function, field)
    return conn.execute(sql, params).fetchone()[0]


def by_name(reverse=False):
    return Query().sorted((Customer.name,), reverse)


def test_empty_query_selects_everything(conn):
    assert set(ids(conn, Query())) == {c.id for c in RECORDS}


def test_filters_add_up(conn):
    query = Query().filter((Customer.city == "Lisbon",)).filter((Customer.name > "Ana",))
    assert set(ids(conn, query)) == {"c3", "c5"}


def test_sorted(conn):
    assert ids(conn, by_name()) == BY_NAME
    assert ids(conn, by_name(reverse=True)) == BY_NAME[::-1]


def test_the_last_sort_is_the_main_one(conn):
    query = Query().sorted((Customer.name,), False).sorted((Customer.age,), False)
    assert ids(conn, query) == ["c3", "c2", "c5", "c1", "c4"]


def test_a_tuple_of_fields_sorts_by_the_first_then_the_rest(conn):
    assert ids(conn, Query().sorted((Customer.city, Customer.age), False)) == ["c1", "c3", "c2", "c5", "c4"]


def test_ties_are_broken_by_key_in_both_directions(conn):
    assert ids(conn, Query().sorted((Customer.city,), False)) == ["c1", "c2", "c3", "c5", "c4"]
    assert ids(conn, Query().sorted((Customer.city,), True)) == ["c4", "c2", "c3", "c5", "c1"]


def test_sorting_by_the_key_needs_no_tiebreaker():
    sql, _ = select_sql(meta(Customer), Query().sorted((Customer.id,), True))
    assert sql.endswith('ORDER BY "id" DESC')


@pytest.mark.parametrize(
    ("start", "stop", "expected"),
    [(1, 3, ["c3", "c4"]), (3, None, ["c1", "c5"]), (0, 2, ["c2", "c3"]), (2, 2, []), (4, 1, []), (9, None, [])],
)
def test_slices(conn, start, stop, expected):
    assert ids(conn, by_name().sliced(start, stop)) == expected


def test_slice_of_a_slice(conn):
    assert ids(conn, by_name().sliced(1, 4).sliced(1, 2)) == ["c4"]


def test_conditions_after_a_slice_apply_to_the_slice(conn):
    query = by_name().sliced(0, 3).filter((Customer.city == "Lisbon",))
    assert ids(conn, query) == ["c2", "c3"]


def test_sort_after_a_slice_reorders_the_slice(conn):
    query = Query().sorted((Customer.id,), False).sliced(0, 3).sorted((Customer.name,), True)
    assert ids(conn, query) == ["c1", "c3", "c2"]


def test_aggregates_cover_the_whole_query(conn):
    everything = Query()
    assert aggregate(conn, everything, "COUNT") == 5
    assert aggregate(conn, everything, "SUM", Customer.age) == 170
    assert aggregate(conn, everything, "MIN", Customer.age) == 20
    assert aggregate(conn, everything, "MAX", Customer.name) == "Eva"


def test_aggregates_cover_only_the_slice(conn):
    first_two = by_name().sliced(0, 2)
    assert aggregate(conn, first_two, "COUNT") == 2
    assert aggregate(conn, first_two, "SUM", Customer.age) == 50


def test_aggregates_of_nothing(conn):
    nothing = Query().filter((Customer.city == "Nowhere",))
    assert aggregate(conn, nothing, "COUNT") == 0
    assert aggregate(conn, nothing, "SUM", Customer.age) is None
    assert aggregate(conn, nothing, "MIN", Customer.age) is None


def test_only_fields_of_the_class_itself_can_be_used():
    with pytest.raises(ValueError, match="Order.total is not a field of Customer"):
        select_sql(meta(Customer), Query().sorted((Order.total,), False))
    with pytest.raises(ValueError, match="Customer.city is not a field of Order"):
        select_sql(meta(Order), Query().sorted((Order.customer.city,), False))
    with pytest.raises(ValueError, match="Order.total is not a field of Customer"):
        aggregate_sql(meta(Customer), Query(), "SUM", Order.total)
