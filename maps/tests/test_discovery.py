from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from mapinator.discovery import (
    Center,
    Criteria,
    CriteriaPatch,
    merge_criteria,
    opening_state,
    retrieve,
)
from mapinator.domain import DomainError
from mapinator.osm import OSMDataset, OSMNode, OSMWay
from tests.memory import MemoryRepository


def discovery_repository() -> MemoryRepository:
    """Synthetic unit fixtures, never production/demo OSM data."""
    tags = {
        "amenity": "cafe",
        "opening_hours": "Mo-Su 22:00-02:00",
        "timezone": "America/Sao_Paulo",
        "internet_access": "wlan",
    }
    nodes = (
        OSMNode(1, -23.55, -46.63, {"name": "Alpha Fixture", **tags}),
        OSMNode(2, -23.55, -46.63, {"name": "Alpha Fixture", **tags}),
        OSMNode(
            3,
            -23.5505,
            -46.63,
            {
                "name": "Unknown Fixture",
                "amenity": "cafe",
                "rating": "5",
                "work_friendly": "yes",
                "quiet": "yes",
            },
        ),
        OSMNode(
            4,
            -23.5506,
            -46.63,
            {"name": "Closed Fixture", **tags, "opening_hours": "Mo-Su 09:00-17:00"},
        ),
        OSMNode(5, -23.551, -46.63, {"name": "Library Fixture", "amenity": "library"}),
        OSMNode(6, -24.0, -46.63, {"name": "Far Fixture", **tags}),
        OSMNode(7, -23.55, -46.631, {}),
        OSMNode(8, -23.551, -46.631, {}),
        OSMNode(9, -23.55, -46.63, {"name": "Not a POI Fixture", "highway": "residential"}),
    )
    ways = (OSMWay(10, (7, 8), {"name": "Way Fixture", "amenity": "cafe"}),)
    repo = MemoryRepository()
    repo.road_data = OSMDataset("synthetic", nodes, ways, (), (-46.7, -24, -46.6, -23))
    return repo


NOW = datetime(2026, 11, 17, 3, 30, tzinfo=UTC)  # Tuesday 00:30 in São Paulo.
CENTER = Center(latitude=-23.55, longitude=-46.63)


@pytest.mark.parametrize(
    ("hours", "zone", "instant", "expected"),
    [
        ("Mo 22:00-02:00", "America/Sao_Paulo", NOW, True),
        ("Mo 22:00-02:00", "UTC", NOW, False),
        ("Mo 22:00-02:00", "America/Sao_Paulo", datetime(2026, 11, 17, 5, 0, tzinfo=UTC), False),
        (
            "Mo-Fr 09:00-12:00,13:00-17:00; Sa 10:00-14:00; Su off",
            "UTC",
            datetime(2026, 11, 16, 12, 30, tzinfo=UTC),
            False,
        ),
        (
            "Mo-Fr 09:00-12:00,13:00-17:00; Sa 10:00-14:00; Su off",
            "UTC",
            datetime(2026, 11, 16, 13, 0, tzinfo=UTC),
            True,
        ),
        ("Fr-Mo 00:00-24:00", "UTC", datetime(2026, 11, 16, 23, 59, tzinfo=UTC), True),
        ("24/7", None, NOW, True),
        ("off", None, NOW, False),
        ("Mo-Su 09:00-18:00", None, NOW, None),
        ("Mo-Su 09:00-18:00", "Unknown/Timezone", NOW, None),
        ("Mo-Su sunrise-sunset", "UTC", NOW, None),
        ("Mo-Fr 09:00-18:00; PH off", "UTC", NOW, None),
        ("Mo-Fr 09:00-18:00; Mo 10:00-12:00", "UTC", NOW, None),
        ("Mo 22:00-02:00; Tu off", "America/Sao_Paulo", NOW, None),
        ("24/7; PH off", "UTC", NOW, None),
        ('Mo 09:00-18:00 "by appointment"', "UTC", NOW, None),
        ("Mo 25:00-26:00", "UTC", NOW, None),
        ("Mo 09:60-17:00", "UTC", NOW, None),
        ("Mo 09:00-09:00", "UTC", NOW, None),
        (None, "UTC", NOW, None),
    ],
)
def test_opening_hours(
    hours: str | None, zone: str | None, instant: datetime, expected: bool | None
) -> None:
    assert opening_state(hours, zone, instant) is expected


def test_dst_and_naive_time() -> None:
    for hour in (5, 6):
        assert (
            opening_state(
                "Su 01:00-02:00",
                "America/New_York",
                datetime(2026, 11, 1, hour, 30, tzinfo=UTC),
            )
            is True
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        opening_state("24/7", None, datetime(2026, 11, 1))


def test_retrieval_uses_osm_nodes_and_ways_without_places() -> None:
    repo = discovery_repository()
    assert repo.road_data is not None and repo.road_data.places == ()
    items, cursor, dataset_id, warnings = retrieve(
        repo, CENTER, Criteria(categories=["cafe"], radius_m=200), NOW, limit=2
    )
    assert [place.source_id for place in items] == ["node/1", "node/2"]
    assert all(place.distance_m == 0 for place in items)
    assert items[0].latitude == -23.55 and items[0].longitude == -46.63
    assert cursor == 2 and dataset_id == UUID(int=1)
    rest, next_cursor, _, _ = retrieve(
        repo, CENTER, Criteria(categories=["cafe"], radius_m=200), NOW, limit=20, cursor=2
    )
    assert next_cursor is None
    assert [p.source_id for p in rest] == ["node/3", "node/4", "way/10"]
    assert rest[-1].location_kind == "osm_way_vertex"
    assert 120 < rest[-1].distance_m < 180
    assert rest[0].rating is None and rest[0].quiet is None and rest[0].work_friendly is None
    assert rest[0].open_at is None and rest[0].wifi is None
    assert "rating" not in rest[0].evidence
    assert warnings


def test_open_now_requires_verified_hours_and_filters_unknown_facts() -> None:
    repo = discovery_repository()
    found, _, _, _ = retrieve(repo, CENTER, Criteria(categories=["cafe"], open_now=True), NOW)
    assert [p.source_id for p in found] == ["node/1", "node/2"]
    assert all(p.open_at is True for p in found)
    for criteria in (
        Criteria(min_rating=0),
        Criteria(work_friendly=True),
        Criteria(quiet=True),
        Criteria(categories=["museum"]),
        Criteria(query="nonexistent"),
    ):
        assert retrieve(repo, CENTER, criteria, NOW)[0] == []
    wifi = retrieve(repo, CENTER, Criteria(wifi=True), NOW)[0]
    assert [p.source_id for p in wifi] == ["node/1", "node/2", "node/4"]
    assert retrieve(repo, CENTER, Criteria(query="library"), NOW)[0][0].category == "library"


def test_empty_and_missing_dataset_and_geodesic_antimeridian() -> None:
    with pytest.raises(DomainError) as failed:
        retrieve(MemoryRepository(), CENTER, Criteria(), NOW)
    assert failed.value.status == 503
    repo = MemoryRepository()
    repo.road_data = OSMDataset(
        "synthetic",
        (OSMNode(1, 0, -179.999, {"name": "Dateline Fixture", "amenity": "cafe"}),),
        (),
        (),
        (-180, 0, 180, 0),
    )
    items = retrieve(repo, Center(latitude=0, longitude=179.999), Criteria(radius_m=300), NOW)[0]
    assert len(items) == 1 and 220 < items[0].distance_m < 225
    assert retrieve(repo, CENTER, Criteria(radius_m=1), NOW)[0] == []


def test_strict_merge_and_schema_bounds() -> None:
    previous = Criteria(categories=["cafe"], open_now=True, wifi=True)
    assert merge_criteria(previous, CriteriaPatch(radius_m=1500)).model_dump() == {
        **previous.model_dump(),
        "radius_m": 1500,
    }
    assert (
        merge_criteria(
            Criteria(query="Alpha", min_rating=4), CriteriaPatch(query=None, min_rating=None)
        ).query
        is None
    )
    for output in (
        {"open_now": "true"},
        {"radius_m": 50_001},
        {"radius_m": float("nan")},
        {"radius_m": None},
        {"categories": ["invented"]},
        {"places": [{"rating": 5}]},
        {"tool": "fetch"},
        {"latitude": -23.55},
    ):
        with pytest.raises(ValidationError):
            CriteriaPatch.model_validate(output)
    with pytest.raises(DomainError):
        merge_criteria(previous, CriteriaPatch(categories=["cafe", "cafe"]))
    with pytest.raises(DomainError):
        merge_criteria(previous, CriteriaPatch(query=" "))
