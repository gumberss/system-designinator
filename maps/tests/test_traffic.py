import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mapinator.routing import build_graph, calculate
from mapinator.traffic import (
    TrafficEstimate,
    TrafficEvent,
    TrafficSample,
    TrafficSnapshot,
    aggregate,
    effective_speed,
    match_observation,
    utc_bucket,
)
from mapinator.traffic_store import contributor_id
from tests.test_routing import network


def event(now: datetime, **values: object) -> TrafficEvent:
    return TrafficEvent.model_validate(
        {
            "event_id": str(uuid4()),
            "session_id": str(uuid4()),
            "contributor": hashlib.sha256(b"test-only-contributor").hexdigest(),
            "graph_version": "synthetic",
            "sequence": 1,
            "observed_at": now,
            "latitude": 0,
            "longitude": 0.002,
            "speed_mps": 3,
            "accuracy_m": 5,
            "heading_deg": 90,
            **values,
        }
    )


def samples(now: datetime, count: int, speed: float = 3) -> list[TrafficSample]:
    return [TrafficSample(str(i), speed, now) for i in range(count)]


def test_map_matching_heading_accuracy_outliers_and_stale_data() -> None:
    now = datetime.now(UTC)
    graph = build_graph(network([(0, 0), (0.004, 0)], [(1, 2, {})]))
    assert match_observation(graph, event(now), now) == "10:0:0:1"
    assert match_observation(graph, event(now, heading_deg=270), now) == "10:0:0:-1"
    for bad in (
        event(now, accuracy_m=36),
        event(now, speed_mps=0),
        event(now, speed_mps=60),
        event(now, heading_deg=0),
        event(now - timedelta(minutes=16)),
        event(now, latitude=0.1),
    ):
        assert match_observation(graph, bad, now) is None


def test_ambiguous_parallel_roads_are_rejected() -> None:
    now = datetime.now(UTC)
    graph = build_graph(
        network(
            [(0, 0), (0.004, 0), (0, 0.00001), (0.004, 0.00001)],
            [(1, 2, {}), (3, 4, {})],
        )
    )
    assert match_observation(graph, event(now), now) is None


def test_minimum_contributors_and_latest_sample_per_contributor() -> None:
    now = datetime.now(UTC)
    assert aggregate("edge", samples(now, 4), 10, now) is None
    duplicated = samples(now, 4) * 10
    assert aggregate("edge", duplicated, 10, now) is None
    estimate = aggregate("edge", samples(now, 5), 10, now)
    assert estimate is not None and estimate.contributors == 5
    assert estimate.speed_mps == 3 and estimate.confidence == 0.5
    assert aggregate("edge", samples(now - timedelta(minutes=6), 10), 10, now) is None


def test_ema_outlier_filter_and_no_repeated_smoothing_of_same_observation() -> None:
    now = datetime.now(UTC)
    previous = TrafficEstimate("edge", 8, now - timedelta(seconds=1), 1, 10, "free")
    observed = samples(now, 5) + [TrafficSample("outlier", 15, now)]
    estimate = aggregate("edge", observed, 10, now, previous)
    assert estimate is not None and estimate.speed_mps == pytest.approx(6.75)
    assert estimate.contributors == 5
    repeated = aggregate("edge", observed, 10, now + timedelta(seconds=30), estimate)
    assert repeated is not None and repeated.speed_mps == estimate.speed_mps
    assert repeated.observed_at == now


def test_confidence_freshness_and_historical_fallback() -> None:
    now = datetime.now(UTC)
    estimate = TrafficEstimate("edge", 2, now, 1, 10, "heavy")
    speed, source = effective_speed(10, estimate, now)
    assert speed == 2 and source == "realtime"
    speed, source = effective_speed(10, estimate, now + timedelta(minutes=6))
    assert speed == 10 and source == "baseline"
    history = replace(estimate, historical_speed_mps=5, historical_confidence=0.5)
    speed, source = effective_speed(10, history, now + timedelta(minutes=6))
    assert speed == pytest.approx(1 / (0.5 / 10 + 0.5 / 5)) and source == "historical"
    low = replace(estimate, confidence=0.5)
    assert effective_speed(10, low, now)[0] > 2


def test_dynamic_traffic_changes_route_without_changing_walking() -> None:
    now = datetime.now(UTC)
    data = network([(0, 0), (0.002, 0), (0.001, 0.001)], [(1, 2, {}), (1, 3, {}), (3, 2, {})])
    baseline = calculate(data, (0, 0), (0.002, 0), "car", "fastest")
    snapshot = TrafficSnapshot(
        "test-snapshot",
        {
            "10:0:0:1": TrafficEstimate("10:0:0:1", 1, now, 1, 10, "heavy"),
        },
    )
    live = calculate(data, (0, 0), (0.002, 0), "car", "fastest", traffic=snapshot, now=now)
    assert live["edge_ids"] != baseline["edge_ids"]
    assert live["traffic_version"] == "test-snapshot"
    walking = calculate(data, (0, 0), (0.002, 0), "walking", "fastest", traffic=snapshot, now=now)
    assert walking["traffic_source"] == "baseline"
    stale = calculate(
        data, (0, 0), (0.002, 0), "car", "fastest", traffic=snapshot, now=now + timedelta(minutes=6)
    )
    assert stale["edge_ids"] == baseline["edge_ids"]


def test_snapshot_applies_to_fractional_snapping_and_versions_are_isolated() -> None:
    now = datetime.now(UTC)
    data = network([(0, 0), (0.004, 0)], [(1, 2, {})])
    snapshot = TrafficSnapshot(
        "test",
        {
            "10:0:0:1": TrafficEstimate("10:0:0:1", 2, now, 1, 10, "heavy"),
            "nonexistent": TrafficEstimate("nonexistent", 1, now, 1, 10, "heavy"),
        },
    )
    result = calculate(data, (0.001, 0), (0.003, 0), "car", "fastest", traffic=snapshot, now=now)
    assert result["traffic_source"] == "realtime"
    assert float(str(result["eta_s"])) == pytest.approx(float(str(result["distance_m"])) / 2)


def test_unavailable_traffic_has_explicit_warning_and_baseline_eta() -> None:
    data = network([(0, 0), (0.004, 0)], [(1, 2, {})])
    result = calculate(
        data, (0.001, 0), (0.003, 0), "car", "fastest", traffic=TrafficSnapshot(unavailable=True)
    )
    assert result["traffic_source"] == "baseline"
    warnings = result["warnings"]
    assert isinstance(warnings, list) and any("unavailable" in warning for warning in warnings)


def test_events_reject_nonfinite_coordinates_and_naive_time() -> None:
    now = datetime.now(UTC)
    for values in (
        {"latitude": "NaN"},
        {"speed_mps": -1},
        {"heading_deg": 360},
        {"observed_at": now.replace(tzinfo=None)},
        {"contributor": "not-hashed"},
    ):
        with pytest.raises(ValidationError):
            event(now, **values)


def test_daily_contributor_does_not_change_when_tracking_session_rotates() -> None:
    now = datetime.now(UTC)
    owner = uuid4()
    assert contributor_id(owner, now) == contributor_id(owner, now)
    assert contributor_id(owner, now) != contributor_id(owner, now + timedelta(days=1))
    assert utc_bucket(datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC)).minute == 55
