import operator

import pytest

from inlay import Ref, all_of, any_of, contains, entity, key, negate, none_of, one_of
from inlay.conditions import And, Compare, FieldRef, Match, Not, OneOf, Or


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


@entity(indexes=[("order",)])
class OrderLine:
    id: str = key()
    order: Ref[Order]


def test_comparisons_build_conditions():
    for fn, op in [
        (operator.eq, "=="),
        (operator.ne, "!="),
        (operator.lt, "<"),
        (operator.le, "<="),
        (operator.gt, ">"),
        (operator.ge, ">="),
    ]:
        cond = fn(Order.total, 100)
        assert isinstance(cond, Compare)
        assert (cond.left.name, cond.op, cond.right) == ("total", op, 100)


def test_value_on_the_left_is_flipped():
    cond = 100 < Order.total
    assert isinstance(cond, Compare)
    assert (cond.left.name, cond.op, cond.right) == ("total", ">", 100)


def test_two_columns_compare():
    cond = Order.total > Order.discount
    assert isinstance(cond, Compare)
    assert isinstance(cond.right, FieldRef)
    assert (cond.left.name, cond.right.name) == ("total", "discount")


def test_related_field_compares_through_its_path():
    cond = Order.customer.city == "Lisbon"
    assert isinstance(cond, Compare)
    assert (cond.left.owner, cond.left.name, cond.left.via) == (Customer, "city", ((Order, "customer"),))


def test_path_compares_by_key():
    cond = Order.customer == "c1"
    assert isinstance(cond, Compare)
    assert (cond.left.owner, cond.left.name, cond.left.via, cond.op, cond.right) == (Order, "customer", (), "==", "c1")

    deep = OrderLine.order.customer != "c1"
    assert isinstance(deep, Compare)
    assert (deep.left.owner, deep.left.name, deep.left.via, deep.op) == (Order, "customer", ((OrderLine, "order"),), "!=")


def test_text_conditions():
    for cond, kind in [
        (Customer.name.startswith("A"), "startswith"),
        (Customer.name.endswith("a"), "endswith"),
        (contains(Customer.name, "n"), "contains"),
    ]:
        assert isinstance(cond, Match)
        assert (cond.field.name, cond.kind) == ("name", kind)


def test_one_of_takes_any_iterable():
    cond = one_of(Customer.city, {"Lisbon"})
    assert isinstance(cond, OneOf)
    assert (cond.field.name, cond.values) == ("city", ("Lisbon",))


def test_combining_conditions():
    a, b = Order.total > 1, Order.total < 9
    both, either = a & b, a | b
    assert isinstance(both, And) and both.items == (a, b)
    assert isinstance(either, Or) and either.items == (a, b)

    assert all_of(a, b).items == (a, b)
    assert any_of(a, b).items == (a, b)
    assert negate(a).item is a
    none = none_of(a, b)
    assert isinstance(none, Not) and isinstance(none.item, Or) and none.item.items == (a, b)


def test_no_conditions():
    assert all_of().items == ()
    assert any_of().items == ()
    assert none_of().item.items == ()


@pytest.mark.parametrize(
    "mistake",
    [
        lambda: (Order.total > 1) and (Order.total < 9),
        lambda: (Order.total > 1) or (Order.total < 9),
        lambda: not (Order.total > 1),
        lambda: 1 < Order.total < 9,
        lambda: Order.total in (1, 2),
    ],
)
def test_python_operators_on_conditions_fail_loudly(mistake):
    with pytest.raises(TypeError, match="no truth value"):
        mistake()
