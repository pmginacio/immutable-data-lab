import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime

import pytest

from inlay import Ref, entity, key
from inlay.meta import FieldMeta, meta
from inlay.nodes import Compare, Filter, In, Table


@entity(indexes=[("city", "name")])
class Customer:
    id: str = key()
    name: str
    city: str


@entity(indexes=[("customer",), ("total",)])
class Order:
    id: str = key()
    customer: Ref[Customer]
    total: int


@entity(indexes=[("parent",)])
class Category:
    id: str = key()
    parent: Ref[Category]
    name: str


def test_key_is_generated_uuid7_or_given():
    ana = Customer(name="Ana", city="Lisbon")
    assert uuid.UUID(ana.id).version == 7
    assert Customer(name="Bob", city="Porto", id="c1").id == "c1"
    assert Customer(name="Ana", city="Lisbon").id != ana.id


def test_replace_keeps_key():
    ana = Customer(name="Ana", city="Lisbon")
    moved = replace(ana, city="Porto")
    assert (moved.id, moved.city) == (ana.id, "Porto")


def test_objects_are_frozen_keyword_only_slotted():
    ana = Customer(name="Ana", city="Lisbon")
    with pytest.raises(FrozenInstanceError):
        ana.city = "Porto"
    with pytest.raises(TypeError):
        Customer("Ana", "Lisbon")
    assert not hasattr(ana, "__dict__")


def test_ref_value_on_object_is_the_key():
    ana = Customer(name="Ana", city="Lisbon")
    assert Order(customer=ana.id, total=1).customer == ana.id


def test_class_access_builds_conditions_on_the_field():
    assert (Order.total == 1) == Compare(meta(Order).field("total"), "==", 1)


def test_self_reference():
    parent, name = meta(Category).field("parent"), meta(Category).field("name")
    grandparent_named = In(parent, Filter(Table(Category), Compare(name, "==", "All")))
    assert (Category.parent.parent.name == "All") == In(parent, Filter(Table(Category), grandparent_named))


def test_unknown_field_raises():
    with pytest.raises(AttributeError):
        Order.totl
    with pytest.raises(AttributeError, match="Customer has no field 'nmae'"):
        Order.customer.nmae
    with pytest.raises(AttributeError, match="Order.total is not a Ref"):
        Order.total.name


def test_meta():
    m = meta(Order)
    id_, customer, total = m.fields
    assert id_ == FieldMeta(Order, "id", str, is_key=True)
    assert customer == FieldMeta(Order, "customer", str, ref=Customer)
    assert total == FieldMeta(Order, "total", int)
    assert (m.cls, m.table, m.key, m.indexes) == (Order, "order", id_, ((customer,), (total,)))
    assert customer.target is Customer


def test_forward_ref_target_resolves_later():
    parent = meta(Category).fields[1]
    assert parent.ref is not Category
    assert parent.target is Category


@pytest.mark.parametrize(
    ("class_name", "table"),
    [("Customer", "customer"), ("OrderLine", "order_line"), ("HTTPLog", "http_log")],
)
def test_default_table_name(class_name, table):
    cls = entity(type(class_name, (), {"__annotations__": {"id": str}, "id": key()}))
    assert meta(cls).table == table


def test_all_field_types_supported():
    @entity
    class Everything:
        id: str = key()
        i: int
        f: float
        s: str
        b: bytes
        flag: bool
        at: datetime
        day: date

    assert [f.py_type for f in meta(Everything).fields][1:] == [int, float, str, bytes, bool, datetime, date]


def test_meta_rejects_plain_class():
    with pytest.raises(TypeError, match="not an @entity"):
        meta(int)


def test_rejects_missing_key():
    with pytest.raises(TypeError, match="exactly one field declared with key"):

        @entity
        class NoKey:
            id: str


def test_rejects_two_keys():
    with pytest.raises(TypeError, match="exactly one field declared with key"):

        @entity
        class TwoKeys:
            id: str = key()
            other: str = key()


def test_rejects_unsupported_type():
    with pytest.raises(TypeError, match=r"Bad.tags has unsupported type"):

        @entity
        class Bad:
            id: str = key()
            tags: list[str]


def test_rejects_unknown_index_field():
    with pytest.raises(TypeError, match="unknown field 'nope'"):

        @entity(indexes=[("nope",)])
        class Bad:
            id: str = key()


def test_rejects_ref_without_index():
    with pytest.raises(TypeError, match=r"Bad.customer is a Ref but no index starts with it"):

        @entity(indexes=[("total", "customer")])
        class Bad:
            id: str = key()
            customer: Ref[Customer]
            total: int


def test_rejects_non_str_key():
    with pytest.raises(TypeError, match="is the key and must be a str"):

        @entity
        class Bad:
            id: int = key()


def test_rejects_field_names_used_by_field_references():
    with pytest.raises(TypeError, match="cannot have fields named startswith"):

        @entity
        class Bad:
            id: str = key()
            startswith: str


def test_rejects_empty_index():
    with pytest.raises(TypeError, match="empty index"):

        @entity(indexes=[()])
        class Bad:
            id: str = key()
