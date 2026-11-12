import operator
from datetime import date, datetime

import pytest

from inlay import Ref, all_of, any_of, contains, entity, key, negate, none_of, one_of, to_day, to_month
from inlay.meta import FieldMeta, meta
from inlay.nodes import And, BinOp, Call, Compare, Filter, In, Match, Not, Or, Table, check_condition


@entity(indexes=[("city",)])
class Customer:
    id: str = key()
    name: str
    city: str


@entity(indexes=[("customer",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int
    discount: int
    placed: datetime


@entity(indexes=[("order",)])
class OrderLine:
    id: str = key()
    order: Ref[Order]


def field(cls: type, name: str) -> FieldMeta:
    return meta(cls).field(name)


def through(cls: type, ref: str, cond) -> In:
    target = meta(cls).field(ref).target
    assert target is not None
    return In(field(cls, ref), Filter(Table(target), cond))


TOTAL, DISCOUNT, CUSTOMER, PLACED = (field(Order, name) for name in ("total", "discount", "customer", "placed"))
NAME, CITY = field(Customer, "name"), field(Customer, "city")


@pytest.mark.parametrize("fn, op", [
    (operator.eq, "=="), (operator.ne, "!="), (operator.lt, "<"),
    (operator.le, "<="), (operator.gt, ">"), (operator.ge, ">="),
])
def test_comparisons_build_compare_nodes(fn, op):
    assert fn(Order.total, 100) == Compare(TOTAL, op, 100)


@pytest.mark.parametrize("built, tree", [
    (100 < Order.total, Compare(TOTAL, ">", 100)),
    (Order.total > Order.discount, Compare(TOTAL, ">", DISCOUNT)),
    (Order.customer == "c1", Compare(CUSTOMER, "==", "c1")),
    (Order.customer.city == "Lisbon", through(Order, "customer", Compare(CITY, "==", "Lisbon"))),
    (Order.customer.city == Order.customer.name, through(Order, "customer", Compare(CITY, "==", NAME))),
    (
        OrderLine.order.customer.city == "Lisbon",
        through(OrderLine, "order", through(Order, "customer", Compare(CITY, "==", "Lisbon"))),
    ),
    (Customer.name.startswith("A"), Match(NAME, "startswith", "A")),
    (Customer.name.endswith("a"), Match(NAME, "endswith", "a")),
    (contains(Customer.name, "n"), Match(NAME, "contains", "n")),
    (Order.customer.name.startswith("A"), through(Order, "customer", Match(NAME, "startswith", "A"))),
    (one_of(Customer.city, {"Lisbon"}), In(CITY, ("Lisbon",))),
    (one_of(Order.customer, ["c1", "c2"]), In(CUSTOMER, ("c1", "c2"))),
    (Order.total * 100 > 5000, Compare(BinOp("*", TOTAL, 100), ">", 5000)),
    (100 * Order.total > 5000, Compare(BinOp("*", 100, TOTAL), ">", 5000)),
    (Order.total - Order.discount > 0, Compare(BinOp("-", TOTAL, DISCOUNT), ">", 0)),
    (to_month(Order.placed) == date(2026, 1, 1), Compare(Call(to_month, PLACED), "==", date(2026, 1, 1))),
    (to_day(Order.placed) < date(2026, 1, 1), Compare(Call(to_day, PLACED), "<", date(2026, 1, 1))),
])
def test_builder_makes_the_tree(built, tree):
    assert built == tree


def test_transforms_compute_in_python_on_values():
    placed = datetime(2026, 3, 5, 23, 30)
    assert (to_month(placed), to_day(placed)) == (date(2026, 3, 1), date(2026, 3, 5))


@pytest.mark.parametrize("mistake, error", [
    (lambda: to_month(Order.total), "needs a date"),
    (lambda: to_month(5), "needs a date"),
    (lambda: Order.customer * 2, "needs numbers"),
    (lambda: Order.total + "x", "needs numbers"),
    (lambda: (Order.total * 2).startswith("1"), "expected a field"),
])
def test_values_of_the_wrong_kind_fail_when_built(mistake, error):
    with pytest.raises(TypeError, match=error):
        mistake()


def test_combining_conditions():
    a, b = Order.total > 1, Order.total < 9
    assert a & b == all_of(a, b) == And((a, b))
    assert a | b == any_of(a, b) == Or((a, b))
    assert negate(a) == Not(a)
    assert none_of(a, b) == Not(Or((a, b)))
    assert (all_of(), any_of(), none_of()) == (And(()), Or(()), Not(Or(())))


@pytest.mark.parametrize("mistake", [lambda: contains("name", "n"), lambda: one_of("city", ["Lisbon"])])
def test_functions_taking_a_field_reject_anything_else(mistake):
    with pytest.raises(TypeError, match="expected a field"):
        mistake()


def test_check_condition_rejects_fields_of_another_class():
    check_condition(Order, (Order.total > 1) & (Order.customer.city == "Lisbon"))
    with pytest.raises(ValueError, match="Order.total cannot be used on Customer"):
        check_condition(Customer, Order.total > 1)


@pytest.mark.parametrize("mistake", [
    lambda: all_of(Order.total > 1, True),
    lambda: any_of(False),
    lambda: negate(True),
    lambda: (Order.total > 1) & True,
])
def test_combinators_reject_a_plain_bool(mistake):
    with pytest.raises(TypeError, match="expected a condition"):
        mistake()


@pytest.mark.parametrize("mistake", [
    lambda: Order.total > Customer.name,
    lambda: Order.customer.city == Customer.city,
    lambda: Order.total + Order.customer.name,
])
def test_only_values_of_the_same_record_combine(mistake):
    with pytest.raises(ValueError, match="same record"):
        mistake()


@pytest.mark.parametrize("mistake", [
    lambda: (Order.total > 1) and (Order.total < 9),
    lambda: (Order.total > 1) or (Order.total < 9),
    lambda: not (Order.total > 1),
    lambda: 1 < Order.total < 9,
    lambda: Order.total in (1, 2),
])
def test_python_operators_on_conditions_fail_loudly(mistake):
    with pytest.raises(TypeError, match="no truth value"):
        mistake()
