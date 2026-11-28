import operator
from datetime import date, datetime

import pytest

from inlay import Field, Ref, all_of, any_of, entity, key, to_day, to_month
from inlay.meta import FieldMeta, meta
from inlay.nodes import And, BinOp, Call, Compare, Filter, In, Match, Not, Or, Table, check_condition


@entity(indexes=[("city",)])
class Customer:
    id: Field[str] = key()
    name: Field[str]
    city: Field[str]


@entity(indexes=[("customer",)])
class Order:
    id: Field[str] = key()
    customer: Ref[Customer]
    total: Field[int]
    discount: Field[int]
    placed: Field[datetime]


@entity(indexes=[("order",)])
class OrderLine:
    id: Field[str] = key()
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
    (Customer.name.contains("n"), Match(NAME, "contains", "n")),
    (Order.customer.name.startswith("A"), through(Order, "customer", Match(NAME, "startswith", "A"))),
    (Customer.city.one_of({"Lisbon"}), In(CITY, ("Lisbon",))),
    (Order.customer.one_of(["c1", "c2"]), In(CUSTOMER, ("c1", "c2"))),
    (Order.total * 100 > 5000, Compare(BinOp("*", TOTAL, 100), ">", 5000)),
    (100 * Order.total > 5000, Compare(BinOp("*", 100, TOTAL), ">", 5000)),
    (Order.total - Order.discount > 0, Compare(BinOp("-", TOTAL, DISCOUNT), ">", 0)),
    (to_month(Order.placed) == date(2026, 1, 1), Compare(Call(to_month, (PLACED,)), "==", date(2026, 1, 1))),
    (to_day(Order.placed) < date(2026, 1, 1), Compare(Call(to_day, (PLACED,)), "<", date(2026, 1, 1))),
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
    (lambda: (Order.total * 2).one_of([2]), "expected a field"),
    (lambda: Order.total > "100", "Order.total is a int, but it is compared with '100', a str"),
    (lambda: Customer.city == 5, "Customer.city is a str, but it is compared with 5"),
    (lambda: Order.total.one_of([1, "2"]), "compared with '2'"),
    (lambda: to_month(Order.placed) == "2026-01-01", "is a date, but it is compared with"),
    (lambda: Order.total.sum(Order.total), "Order.total is not a Ref of Order"),
    (lambda: Order.total.sum(Customer.id), "Customer.id is not a Ref of Order"),
    (lambda: Customer.name.sum(), "sum cannot take Customer.name, a str"),
])
def test_values_of_the_wrong_kind_fail_when_built(mistake, error):
    with pytest.raises(TypeError, match=error):
        mistake()


def test_combining_conditions():
    a, b = Order.total > 1, Order.total < 9
    assert a & b == all_of(a, b) == And((a, b))
    assert a | b == any_of(a, b) == Or((a, b))
    assert ~a == Not(a)
    assert (all_of(), any_of()) == (And(()), Or(()))


def test_an_aggregate_filters_only_the_records_it_aggregates():
    Order.total.sum(Order.customer, filter=Order.discount > 0)
    with pytest.raises(ValueError, match="Customer.city cannot be used on Order"):
        Order.total.sum(filter=Customer.city == "Lisbon")


def test_check_condition_rejects_fields_of_another_class():
    check_condition(Order, (Order.total > 1) & (Order.customer.city == "Lisbon"))
    with pytest.raises(ValueError, match="Order.total cannot be used on Customer"):
        check_condition(Customer, Order.total > 1)


@pytest.mark.parametrize("mistake", [
    lambda: all_of(Order.total > 1, True),
    lambda: any_of(False),
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
