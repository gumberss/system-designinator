"""Synthetic OSM fixtures; not real charging stations or battery telemetry."""

import math
import pickle
import sys
import time
from dataclasses import replace
from typing import cast

import pytest
from pydantic import ValidationError

from mapinator.domain import DomainError
from mapinator.ev import (
    Budget,
    PlanningLimits,
    Vehicle,
    VehicleRequest,
    charging_seconds,
    charging_stations,
    plan_ev_trip,
)
from mapinator.osm import OSMDataset, OSMRelation, OSMWay
from mapinator.routing import build_graph
from tests.test_routing import network


def profile(**changes: object) -> VehicleRequest:
    return VehicleRequest.model_validate(
        {
            "battery_capacity_kwh": 5.0,
            "initial_soc_percent": 90.0,
            "reserve_percent": 10.0,
            "consumption_kwh_per_100km": 20.0,
            "max_charging_power_kw": 50.0,
            "connectors": ["type2_combo"],
            **changes,
        }
    )


def regional_fixture(chargers: dict[int, dict[str, str]] | None = None) -> OSMDataset:
    data = network(
        [(0, 0), (0.1, 0), (0.2, 0), (0.3, 0)],
        [(1, 2, {}), (2, 3, {}), (3, 4, {})],
    )
    tags = (
        chargers
        if chargers is not None
        else {
            2: {"socket:type2_combo": "2", "socket:type2_combo:output": "50 kW"},
            3: {"socket:type2_combo": "1", "socket:type2_combo:output": "50000 W"},
        }
    )
    return replace(
        data,
        nodes=tuple(
            replace(
                node,
                tags={
                    "amenity": "charging_station",
                    "name": f"Synthetic charger {node.osm_id}",
                    **tags[node.osm_id],
                },
            )
            if node.osm_id in tags
            else node
            for node in data.nodes
        ),
    )


def legs(result: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], result["legs"])


def stops(result: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], result["charging_stops"])


def test_multistop_reserve_every_leg_and_energy_conservation() -> None:
    result = plan_ev_trip(regional_fixture(), (0, 0), (0.3, 0), profile())
    assert result["ev_planning_available"] is True and result["feasible"] is True
    assert [stop["source_id"] for stop in stops(result)] == ["node/2", "node/3"]
    assert len(legs(result)) == 3
    previous_arrival = 90.0
    for leg in legs(result):
        energy = cast(dict[str, float], leg["energy"])
        departure = cast(float, leg["departure_soc_percent"])
        arrival = cast(float, leg["arrival_soc_percent"])
        assert arrival >= 10.0 - 1e-8
        assert cast(float, leg["minimum_soc_percent"]) >= 10.0 - 1e-8
        assert departure >= previous_arrival - 1e-8
        assert departure - arrival == pytest.approx(energy["upper_kwh"] / 5 * 100)
        assert energy["lower_kwh"] < energy["estimate_kwh"] < energy["upper_kwh"]
        assert cast(dict[str, float], leg["arrival_soc_interval_percent"])["upper"] >= arrival
        previous_arrival = arrival
    assert result["total_time_s"] == pytest.approx(
        cast(float, result["driving_s"])
        + cast(float, result["access_s"])
        + cast(float, result["charging_s"])
    )
    assert all(stop["availability"] == "unknown" for stop in stops(result))
    assert all(
        set(cast(dict[str, float], stop["coordinate"])) == {"latitude", "longitude"}
        for stop in stops(result)
    )
    assert cast(dict[str, object], result["provenance"])["graph_version"] == "synthetic"


def test_reserve_cannot_be_borrowed_to_reach_first_charger() -> None:
    # Nominal energy fits 60% SOC, upper energy plus reserve does not.
    with pytest.raises(DomainError) as error:
        plan_ev_trip(regional_fixture(), (0, 0), (0.3, 0), profile(initial_soc_percent=60.0))
    assert error.value.code == "ev_trip_unreachable"


def test_soc_bucket_rounding_does_not_discard_feasible_initial_state() -> None:
    result = plan_ev_trip(
        regional_fixture(),
        (0, 0),
        (0.1, 0),
        profile(initial_soc_percent=70.0),
    )
    assert result["feasible"] is True
    assert cast(float, result["destination_soc_percent"]) >= 10
    assert not result["charging_stops"]


def test_incompatible_connector_does_not_make_itinerary_feasible() -> None:
    data = regional_fixture(
        {
            2: {"socket:chademo": "1", "socket:chademo:output": "50 kW"},
            3: {"socket:chademo": "1", "socket:chademo:output": "50 kW"},
        }
    )
    with pytest.raises(DomainError) as error:
        plan_ev_trip(data, (0, 0), (0.3, 0), profile())
    assert error.value.code == "ev_trip_unreachable"


def test_unknown_power_access_and_availability_never_invented() -> None:
    data = regional_fixture(
        {
            2: {"socket:type2_combo": "yes", "socket:type2_combo:output": "unknown"},
            3: {"socket:type2_combo": "yes", "socket:type2_combo:output": "50 kW"},
        }
    )
    stations, excluded = charging_stations(data, build_graph(data))
    assert [station.source_id for station in stations] == ["node/3"]
    assert excluded == [{"source_id": "node/2", "reason": "connector_power_unknown"}]
    response = stations[0].response()
    assert response["availability"] == "unknown"
    assert response["availability_observed_at"] is None
    assert response["opening_hours"] is None and response["provenance"]
    unconnected = replace(data, ways=data.ways[:1])
    _, excluded_access = charging_stations(unconnected, build_graph(unconnected))
    assert {"source_id": "node/3", "reason": "car_access_unverified"} in excluded_access


def test_way_charger_uses_real_connected_member_and_explicit_power_units() -> None:
    data = regional_fixture({})
    data = replace(
        data,
        ways=data.ways
        + (
            OSMWay(
                100,
                (2, 3),
                {
                    "amenity": "charging_station",
                    "socket:type2_combo": "2",
                    "socket:type2_combo:output": "0.05 MW",
                    "name": "Synthetic mapped site",
                },
            ),
        ),
    )
    stations, excluded = charging_stations(data, build_graph(data))
    assert not excluded and len(stations) == 1
    assert stations[0].source_id == "way/100" and stations[0].point == (0.1, 0)
    assert stations[0].connectors == (("type2_combo", 50.0),)
    assert stations[0].location_kind == "mapped_site_road_member"
    ambiguous = replace(
        data,
        ways=data.ways[:-1]
        + (replace(data.ways[-1], tags={**data.ways[-1].tags, "socket:type2_combo:output": "50"}),),
    )
    assert charging_stations(ambiguous, build_graph(ambiguous))[0] == []


def test_taper_integration_and_vehicle_station_power_limits() -> None:
    vehicle = Vehicle.from_request(profile())
    low = charging_seconds(vehicle, 50, 0.2, 0.4)
    high = charging_seconds(vehicle, 50, 0.8, 1)
    assert high[1] > low[1] and high[0] == pytest.approx(low[0])
    assert charging_seconds(vehicle, 10, 0.2, 0.4)[0] == pytest.approx(360)
    calibrated = Vehicle.from_request(
        profile(
            calibrated_charging_curve=[
                {"soc_percent": 0.0, "power_kw": 50.0},
                {"soc_percent": 80.0, "power_kw": 50.0},
                {"soc_percent": 100.0, "power_kw": 10.0},
            ]
        )
    )
    exact = charging_seconds(calibrated, 100, 0.8, 1)
    assert exact[0] == pytest.approx(exact[1])
    assert exact[0] == pytest.approx(5 * math.log(5) / 200 * 3600)


def test_time_search_skips_slow_nearby_charger_not_greedy_nearest() -> None:
    data = regional_fixture(
        {
            2: {"socket:type2_combo": "1", "socket:type2_combo:output": "1 kW"},
            3: {"socket:type2_combo": "1", "socket:type2_combo:output": "50 kW"},
        }
    )
    result = plan_ev_trip(
        data,
        (0, 0),
        (0.3, 0),
        profile(battery_capacity_kwh=10.0, initial_soc_percent=75.0),
    )
    assert [stop["source_id"] for stop in stops(result)] == ["node/3"]
    assert cast(float, stops(result)[0]["departure_soc_percent"]) < 80


def test_missing_or_partial_vehicle_is_only_ordinary_route_fallback() -> None:
    for vehicle in (None, VehicleRequest(battery_capacity_kwh=10.0)):
        result = plan_ev_trip(regional_fixture(), (0, 0), (0.3, 0), vehicle)
        assert result["ev_planning_available"] is False and result["feasible"] is None
        assert result["fallback_reason"] == "missing_vehicle_parameters"
        assert result["missing_vehicle_parameters"] and result["charging_stops"] == []
        route = cast(dict[str, object], result["ordinary_route"])
        assert route["mode"] == "car" and route["algorithm"] == "hierarchical"
        assert route["geometry"] and route["edge_ids"]


@pytest.mark.parametrize(
    "change",
    [
        {"battery_capacity_kwh": 0.0},
        {"consumption_kwh_per_100km": -1.0},
        {"max_charging_power_kw": 0.0},
        {"initial_soc_percent": 101.0},
        {"reserve_percent": 91.0},
        {"connectors": []},
        {"connectors": ["invented"]},
        {"battery_capacity_kwh": "10"},
        {"availability": "free"},
        {
            "calibrated_charging_curve": [
                {"soc_percent": 20.0, "power_kw": 10.0},
                {"soc_percent": 100.0, "power_kw": 10.0},
            ]
        },
    ],
)
def test_vehicle_rejects_invalid_or_coerced_parameters(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        profile(**change)


def test_unreachable_destination_and_oneway_use_existing_errors() -> None:
    data = network([(0, 0), (0.01, 0)], [(1, 2, {"oneway": "yes"})])
    with pytest.raises(DomainError) as error:
        plan_ev_trip(data, (0.01, 0), (0, 0), profile())
    assert error.value.code == "route_not_found"


def test_turn_restriction_survives_charging_leg_boundary() -> None:
    rule = OSMRelation(
        1,
        (("way", 10, "from"), ("node", 2, "via"), ("way", 11, "to")),
        {"type": "restriction", "restriction": "no_right_turn"},
    )
    data = network(
        [(0, 0), (0.1, 0), (0.1, -0.1), (0.2, 0), (0.2, -0.1)],
        [(1, 2, {}), (2, 3, {}), (2, 4, {}), (4, 5, {}), (5, 3, {})],
        (rule,),
    )
    data = replace(
        data,
        nodes=tuple(
            replace(
                node,
                tags={
                    "amenity": "charging_station",
                    "socket:type2_combo": "1",
                    "socket:type2_combo:output": "50 kW",
                },
            )
            if node.osm_id == 2
            else node
            for node in data.nodes
        ),
    )
    # Breaking at charger 2 must not reset the forbidden right turn to a fake shortcut.
    with pytest.raises(DomainError) as error:
        plan_ev_trip(data, (0, 0), (0.1, -0.1), profile())
    assert error.value.code == "ev_trip_unreachable"


def test_shared_wall_deadline_and_routing_cap_restore_trace() -> None:
    data = regional_fixture()
    budget = Budget(PlanningLimits(wall_seconds=0.000001), time.monotonic() - 1)
    previous = sys.gettrace()
    with pytest.raises(DomainError) as error:
        budget.route(data, (0, 0), (0.3, 0), "fastest", None, None)
    assert error.value.code == "ev_budget_exceeded" and sys.gettrace() is previous
    with pytest.raises(DomainError) as interrupted:
        plan_ev_trip(
            data, (0, 0), (0.3, 0), profile(), limits=PlanningLimits(wall_seconds=0.000001)
        )
    assert interrupted.value.code == "ev_budget_exceeded" and sys.gettrace() is previous
    with pytest.raises(DomainError) as capped:
        plan_ev_trip(data, (0, 0), (0.3, 0), profile(), limits=PlanningLimits(route_call_cap=1))
    assert capped.value.code == "ev_budget_exceeded"


def test_shared_deadline_interrupts_inflight_python_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_calculate(*args: object, **kwargs: object) -> dict[str, object]:
        end = time.monotonic() + 0.2
        while time.monotonic() < end:
            pass
        return {}

    monkeypatch.setattr("mapinator.ev.calculate", slow_calculate)
    previous = sys.gettrace()
    started = time.monotonic()
    with pytest.raises(DomainError) as error:
        plan_ev_trip(
            regional_fixture(),
            (0, 0),
            (0.3, 0),
            profile(),
            limits=PlanningLimits(wall_seconds=0.01),
        )
    assert error.value.code == "ev_budget_exceeded"
    assert time.monotonic() - started < 0.15
    assert sys.gettrace() is previous


def test_state_queue_is_bounded_not_just_the_router_calls() -> None:
    with pytest.raises(DomainError) as error:
        plan_ev_trip(
            regional_fixture(),
            (0, 0),
            (0.3, 0),
            profile(),
            limits=PlanningLimits(state_cap=1),
        )
    assert error.value.code == "ev_budget_exceeded"


def test_candidate_truncation_is_explicit_and_entrypoint_is_pickleable() -> None:
    data, vehicle = pickle.loads(
        pickle.dumps(
            (
                regional_fixture(),
                profile(battery_capacity_kwh=100.0),
            )
        )
    )
    function = pickle.loads(pickle.dumps(plan_ev_trip))
    result = function(data, (0, 0), (0.3, 0), vehicle, limits=PlanningLimits(candidate_cap=1))
    assert cast(dict[str, object], result["search"])["candidates_truncated"] is True
    assert any("candidate cap" in warning for warning in cast(list[str], result["warnings"]))
    assert pickle.loads(pickle.dumps(result)) == result
