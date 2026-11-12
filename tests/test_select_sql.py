import sqlite3

import pytest

from inlay import entity, key
from inlay.meta import meta
from inlay.nodes import Filter, Slice, Sort, Table
from inlay.sql import aggregate_sql, create_table_sql, insert_sql, select_sql


@entity(indexes=[("city",)])
class Customer:
    id: str = key()
    name: str
    city: str
    age: int


ID, NAME, CITY, AGE = (meta(Customer).field(name) for name in ("id", "name", "city", "age"))
CUSTOMERS = Table(Customer)

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
    sql, params = select_sql(query)
    return [row[0] for row in conn.execute(sql, params)]


def aggregate(conn, query, func, value=None):
    sql, params = aggregate_sql(query, func, value)
    return conn.execute(sql, params).fetchone()[0]


def by_name(reverse=False):
    return Sort(CUSTOMERS, (NAME,), reverse)


def test_empty_query_selects_everything(conn):
    assert set(ids(conn, CUSTOMERS)) == {c.id for c in RECORDS}


def test_filters_add_up(conn):
    query = Filter(Filter(CUSTOMERS, Customer.city == "Lisbon"), Customer.name > "Ana")
    assert set(ids(conn, query)) == {"c3", "c5"}


def test_sorted(conn):
    assert ids(conn, by_name()) == BY_NAME
    assert ids(conn, by_name(reverse=True)) == BY_NAME[::-1]


def test_the_last_sort_is_the_main_one(conn):
    query = Sort(Sort(CUSTOMERS, (NAME,), False), (AGE,), False)
    assert ids(conn, query) == ["c3", "c2", "c5", "c1", "c4"]


def test_a_tuple_of_fields_sorts_by_the_first_then_the_rest(conn):
    assert ids(conn, Sort(CUSTOMERS, (CITY, AGE), False)) == ["c1", "c3", "c2", "c5", "c4"]


def test_ties_are_broken_by_key_in_both_directions(conn):
    assert ids(conn, Sort(CUSTOMERS, (CITY,), False)) == ["c1", "c2", "c3", "c5", "c4"]
    assert ids(conn, Sort(CUSTOMERS, (CITY,), True)) == ["c4", "c2", "c3", "c5", "c1"]


def test_sorting_by_the_key_needs_no_tiebreaker():
    sql, _ = select_sql(Sort(CUSTOMERS, (ID,), True))
    assert sql.endswith('ORDER BY "id" DESC')


@pytest.mark.parametrize(
    ("start", "stop", "expected"),
    [(1, 3, ["c3", "c4"]), (3, None, ["c1", "c5"]), (0, 2, ["c2", "c3"]), (2, 2, []), (4, 1, []), (9, None, [])],
)
def test_slices(conn, start, stop, expected):
    assert ids(conn, Slice(by_name(), start, stop)) == expected


def test_slice_of_a_slice(conn):
    assert ids(conn, Slice(Slice(by_name(), 1, 4), 1, 2)) == ["c4"]


def test_conditions_after_a_slice_apply_to_the_slice(conn):
    query = Filter(Slice(by_name(), 0, 3), Customer.city == "Lisbon")
    assert ids(conn, query) == ["c2", "c3"]


def test_sort_after_a_slice_reorders_the_slice(conn):
    query = Sort(Slice(Sort(CUSTOMERS, (ID,), False), 0, 3), (NAME,), True)
    assert ids(conn, query) == ["c1", "c3", "c2"]


def test_aggregates_cover_the_whole_query(conn):
    everything = CUSTOMERS
    assert aggregate(conn, everything, len) == 5
    assert aggregate(conn, everything, sum, AGE) == 170
    assert aggregate(conn, everything, min, AGE) == 20
    assert aggregate(conn, everything, max, NAME) == "Eva"


def test_aggregates_cover_only_the_slice(conn):
    first_two = Slice(by_name(), 0, 2)
    assert aggregate(conn, first_two, len) == 2
    assert aggregate(conn, first_two, sum, AGE) == 50


def test_aggregates_of_nothing(conn):
    nothing = Filter(CUSTOMERS, Customer.city == "Nowhere")
    assert aggregate(conn, nothing, len) == 0
    assert aggregate(conn, nothing, sum, AGE) is None
    assert aggregate(conn, nothing, min, AGE) is None
