import operator

import pytest

from inlay import Ref, all_of, any_of, contains, entity, key, negate, none_of, one_of
from inlay.conditions import And, Compare, FieldRef, Match, Not, OneOf, Or, as_field, check_condition


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


@pytest.mark.parametrize(
    "mistake",
    [
        lambda: contains("name", "n"),
        lambda: one_of("city", ["Lisbon"]),
    ],
)
def test_functions_taking_a_field_reject_anything_else(mistake):
    with pytest.raises(TypeError, match="expected a field"):
        mistake()


def test_a_relation_stands_for_its_ref_field():
    ref = as_field(OrderLine.order.customer)
    assert (ref.owner, ref.name, ref.via) == (Order, "customer", ((OrderLine, "order"),))
    cond = one_of(Order.customer, ["c1", "c2"])
    assert (cond.field.owner, cond.field.name, cond.values) == (Order, "customer", ("c1", "c2"))


def test_check_condition_rejects_fields_of_another_class():
    check_condition(Order, (Order.total > 1) & (Order.customer.city == "Lisbon"))
    with pytest.raises(ValueError, match="Order.total cannot be used on Customer"):
        check_condition(Customer, Order.total > 1)


def test_check_condition_rejects_a_plain_bool():
    with pytest.raises(TypeError, match="expected a condition"):
        check_condition(Order, all_of(True))


@pytest.mark.parametrize(
    "mistake",
    [lambda: Order.total > Customer.name, lambda: Order.customer.city == Customer.city],
)
def test_only_fields_of_the_same_record_compare(mistake):
    with pytest.raises(ValueError, match="same record"):
        mistake()


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
