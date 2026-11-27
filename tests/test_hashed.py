"""Keys computed from the other fields, for values stored once however many records point to them."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

import inlay
from inlay import Field, Ref, entity, default, hashed


@entity
class Place:
    id: Field[str] = hashed()
    town: Field[str]
    seen: Field[datetime | None] = default(None)

    def __post_init__(self) -> None:
        if not self.town:
            raise ValueError(f"a place needs a town, got key {self.id}")


@entity(indexes=[("place",)])
class Listing:
    id: Field[str] = hashed()
    price: Field[int]
    place: Ref[Place]


def test_the_same_values_give_the_same_key():
    assert Place(town="Faro").id == Place(town="Faro").id
    assert Place(town="Faro").id != Place(town="Tavira").id
    assert replace(Place(town="Faro"), town="Tavira").id == Place(town="Tavira").id


def test_a_datetime_gives_the_same_key_in_any_time_zone():
    at = datetime(2026, 8, 1, 3, tzinfo=UTC)
    assert Place(town="Faro", seen=at).id == Place(town="Faro", seen=at.astimezone(timezone(timedelta(hours=1)))).id


def test_a_record_pointing_to_another_changes_key_with_it():
    faro, tavira = Place(town="Faro"), Place(town="Tavira")
    assert Listing(price=1, place=faro.id).id != Listing(price=1, place=tavira.id).id


def test_the_class_own_post_init_runs_after_the_key_is_set():
    with pytest.raises(ValueError, match="a place needs a town, got key [0-9a-f]{64}"):
        Place(town="")


def test_writing_the_same_values_again_keeps_one_record(tmp_path):
    with inlay.open(tmp_path / "places.db") as db:
        db.register(Place, Listing)
        faro = Place(town="Faro")
        db.update(faro, Listing(price=1, place=faro.id))
        db.update(Place(town="Faro"), Listing(price=1, place=Place(town="Faro").id))
        assert (list(db[Place]), len(db[Listing])) == ([faro], 1)
