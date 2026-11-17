"""Bounded regional EV planning over the imported OSM road network."""

import heapq
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from types import FrameType
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mapinator.domain import DomainError
from mapinator.osm import OSMDataset
from mapinator.routing import Graph, Point, build_graph, calculate, distance
from mapinator.traffic import TrafficSnapshot, apply_snapshot

if TYPE_CHECKING:
    from _typeshed import TraceFunction

Connector = Literal[
    "type1",
    "type1_combo",
    "type2",
    "type2_cable",
    "type2_combo",
    "chademo",
    "tesla_supercharger",
    "tesla_destination",
]
CONNECTORS = {
    "type1",
    "type1_combo",
    "type2",
    "type2_cable",
    "type2_combo",
    "chademo",
    "tesla_supercharger",
    "tesla_destination",
}
REQUIRED = (
    "battery_capacity_kwh",
    "initial_soc_percent",
    "reserve_percent",
    "consumption_kwh_per_100km",
    "max_charging_power_kw",
    "connectors",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ChargeCurvePoint(StrictModel):
    soc_percent: float = Field(ge=0, le=100)
    power_kw: float = Field(gt=0)


class VehicleRequest(StrictModel):
    # Partial profiles are accepted only to produce an explicit ordinary-route fallback.
    battery_capacity_kwh: float | None = Field(default=None, gt=0, le=1000)
    initial_soc_percent: float | None = Field(default=None, ge=0, le=100)
    reserve_percent: float | None = Field(default=None, ge=0, lt=100)
    consumption_kwh_per_100km: float | None = Field(default=None, gt=0, le=1000)
    max_charging_power_kw: float | None = Field(default=None, gt=0, le=2000)
    connectors: list[Connector] | None = Field(default=None, min_length=1, max_length=8)
    calibrated_charging_curve: list[ChargeCurvePoint] | None = Field(
        default=None, min_length=2, max_length=32
    )

    @model_validator(mode="after")
    def validate_profile(self) -> "VehicleRequest":
        if (
            self.initial_soc_percent is not None
            and self.reserve_percent is not None
            and self.initial_soc_percent < self.reserve_percent
        ):
            raise ValueError("Initial SOC must be at least the reserve.")
        curve = self.calibrated_charging_curve
        if curve:
            if curve[0].soc_percent != 0 or curve[-1].soc_percent != 100:
                raise ValueError("The calibrated curve must cover SOC 0 through 100.")
            if any(a.soc_percent >= b.soc_percent for a, b in zip(curve, curve[1:], strict=False)):
                raise ValueError("Calibrated curve SOC points must be strictly increasing.")
            if self.max_charging_power_kw is not None and any(
                p.power_kw > self.max_charging_power_kw for p in curve
            ):
                raise ValueError("Calibrated power cannot exceed vehicle maximum power.")
        return self

    def missing(self) -> list[str]:
        return [key for key in REQUIRED if getattr(self, key) is None]


@dataclass(frozen=True)
class Vehicle:
    capacity: float
    initial: float
    reserve: float
    consumption: float
    maximum_power: float
    connectors: frozenset[str]
    curve: tuple[tuple[float, float], ...] | None

    @classmethod
    def from_request(cls, value: VehicleRequest) -> "Vehicle":
        if value.missing():
            raise DomainError(422, "incomplete_vehicle", "Vehicle parameters are incomplete.")
        return cls(
            cast(float, value.battery_capacity_kwh),
            cast(float, value.initial_soc_percent) / 100,
            cast(float, value.reserve_percent) / 100,
            cast(float, value.consumption_kwh_per_100km),
            cast(float, value.max_charging_power_kw),
            frozenset(cast(list[str], value.connectors)),
            tuple((p.soc_percent / 100, p.power_kw) for p in value.calibrated_charging_curve)
            if value.calibrated_charging_curve
            else None,
        )


@dataclass(frozen=True)
class PlanningLimits:
    wall_seconds: float = 6
    candidate_cap: int = 12
    route_call_cap: int = 384
    state_cap: int = 20_000
    stop_cap: int = 6
    soc_step: float = 0.05

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.wall_seconds)
            or not 0 < self.wall_seconds < 30
            or not 1 <= self.candidate_cap <= 24
            or not 1 <= self.route_call_cap <= 512
            or not 1 <= self.state_cap <= 100_000
            or not 1 <= self.stop_cap <= 12
            or not math.isfinite(self.soc_step)
            or not 0.01 <= self.soc_step <= 0.25
        ):
            raise ValueError("Invalid regional EV resource limits.")


@dataclass
class Budget:
    limits: PlanningLimits
    deadline: float
    route_calls: int = 0
    expanded_states: int = 0
    trace_events: int = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise DomainError(503, "ev_budget_exceeded", "Shared EV wall deadline reached.")

    def trace(self, frame: FrameType, event: str, arg: object) -> "TraceFunction":
        self.trace_events += 1
        if self.trace_events % 64 == 0:
            self.check()
        return self.trace

    def route(
        self,
        dataset: OSMDataset,
        source: Point,
        destination: Point,
        strategy: str,
        traffic: TrafficSnapshot | None,
        now: datetime | None,
    ) -> dict[str, object]:
        self.check()
        if self.route_calls >= self.limits.route_call_cap:
            raise DomainError(503, "ev_budget_exceeded", "Shared EV routing call cap reached.")
        self.route_calls += 1
        previous = sys.gettrace()
        # calculate has its own per-route deadline. Trace its Python work as well so graph
        # construction/snapping and successive calls cannot each restart the total budget.
        sys.settrace(self.trace)
        try:
            result = calculate(
                dataset,
                source,
                destination,
                "car",
                cast(Literal["fastest", "shortest"], strategy),
                traffic=traffic,
                now=now,
            )
        finally:
            sys.settrace(previous)
        self.check()
        return result


@dataclass(frozen=True)
class Station:
    source_id: str
    name: str | None
    point: Point
    connectors: tuple[tuple[str, float], ...]
    tags: dict[str, str]
    location_kind: str

    def compatible_power(self, vehicle: Vehicle) -> tuple[str, float] | None:
        compatible = [
            (kind, power) for kind, power in self.connectors if kind in vehicle.connectors
        ]
        return max(compatible, key=lambda item: item[1]) if compatible else None

    def response(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "coordinate": {"latitude": self.point[1], "longitude": self.point[0]},
            "location_kind": self.location_kind,
            "connectors": [
                {"connector": kind, "power_kw": power} for kind, power in self.connectors
            ],
            "availability": "unknown",
            "availability_observed_at": None,
            "opening_hours": self.tags.get("opening_hours"),
            "provenance": {
                "source": "OpenStreetMap",
                "source_id": self.source_id,
                "license": "ODbL 1.0; © OpenStreetMap contributors",
                "tags": self.tags,
            },
        }


def _power(raw: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(kW|W|MW)\s*", raw, re.IGNORECASE)
    if not match:
        return None
    value = float(match[1]) * {"w": 0.001, "kw": 1, "mw": 1000}[match[2].lower()]
    return value if math.isfinite(value) and 0 < value <= 2000 else None


def _sockets(tags: dict[str, str]) -> tuple[tuple[str, float], ...]:
    result = []
    for connector in sorted(CONNECTORS):
        count_tag = tags.get(f"socket:{connector}")
        if count_tag is not None and not (
            count_tag == "yes" or (count_tag.isdecimal() and int(count_tag) > 0)
        ):
            continue
        power = _power(tags.get(f"socket:{connector}:output", ""))
        if power is not None:
            result.append((connector, power))
    return tuple(result)


def charging_stations(
    dataset: OSMDataset,
    graph: Graph,
    budget: Budget | None = None,
) -> tuple[list[Station], list[dict[str, str]]]:
    """Use mapped car-connected nodes/site members; never invent driveway access."""
    car_nodes = {
        node
        for edge in graph.edges.values()
        if "car" in edge.speeds
        for node in (edge.source, edge.target)
    }
    nodes = {node.osm_id: node for node in dataset.nodes}
    stations: list[Station] = []
    excluded: list[dict[str, str]] = []
    elements = [
        (f"node/{n.osm_id}", n.tags, (n.osm_id,), "osm_node")
        for n in dataset.nodes
        if n.tags.get("amenity") == "charging_station"
    ] + [
        (f"way/{w.osm_id}", w.tags, w.refs, "mapped_site_road_member")
        for w in dataset.ways
        if w.tags.get("amenity") == "charging_station"
    ]
    for identifier, tags, refs, location_kind in elements:
        if budget:
            budget.check()
        sockets = _sockets(tags)
        access = next(
            (
                tags[key]
                for key in ("motorcar", "motor_vehicle", "vehicle", "access")
                if key in tags
            ),
            None,
        )
        candidates = [nodes[ref] for ref in refs if ref in nodes and ref in car_nodes]
        reason = (
            "access_not_public"
            if access not in {None, "yes", "permissive", "designated"}
            else "conditional_access_unknown"
            if any(k.endswith(":conditional") for k in tags)
            else "station_out_of_service"
            if tags.get("operational_status") in {"closed", "out_of_order", "inactive"}
            or tags.get("disused") == "yes"
            else "connector_power_unknown"
            if not sockets
            else "car_access_unverified"
            if not candidates
            else None
        )
        if reason:
            excluded.append({"source_id": identifier, "reason": reason})
            continue
        node = min(candidates, key=lambda n: n.osm_id)
        stations.append(
            Station(
                identifier,
                tags.get("name"),
                (node.longitude, node.latitude),
                sockets,
                dict(tags),
                location_kind,
            )
        )
    return stations, excluded


def charging_seconds(
    vehicle: Vehicle,
    station_power: float,
    arrival: float,
    departure: float,
) -> tuple[float, float]:
    """Integrate capacity / min(station, vehicle, interpolated curve power)."""
    if not 0 <= arrival <= departure <= 1 or station_power <= 0:
        raise ValueError("Invalid charging interval or station power.")
    maximum = min(vehicle.maximum_power, station_power)
    curve = vehicle.curve or (
        (0, vehicle.maximum_power),
        (0.8, vehicle.maximum_power),
        (1, vehicle.maximum_power * 0.2),
    )
    hours = 0.0
    for (left, p_left), (right, p_right) in zip(curve, curve[1:], strict=False):
        start, end = max(left, arrival), min(right, departure)
        if start >= end:
            continue
        slope = (p_right - p_left) / (right - left)
        points = [start, end]
        if slope:
            crossing = left + (maximum - p_left) / slope
            if start < crossing < end:
                points.insert(1, crossing)
        for a, b in zip(points, points[1:], strict=False):
            pa, pb = p_left + slope * (a - left), p_left + slope * (b - left)
            if (pa + pb) / 2 >= maximum:
                hours += vehicle.capacity * (b - a) / maximum
            elif abs(slope) < 1e-12:
                hours += vehicle.capacity * (b - a) / pa
            else:
                hours += vehicle.capacity * math.log(pb / pa) / slope
    seconds = hours * 3600
    if vehicle.curve is not None:
        return seconds, seconds
    ideal = vehicle.capacity * (departure - arrival) / maximum * 3600
    return ideal, seconds * 1.3


def energy_interval(
    route: dict[str, object],
    vehicle: Vehicle,
    graph: Graph,
) -> dict[str, float]:
    metres = cast(float, route["distance_m"])
    access = cast(float, route["source_snap_m"]) + cast(float, route["destination_snap_m"])
    geometry = cast(dict[str, object], route["geometry"])
    points = cast(list[Point], geometry["coordinates"])
    weighted_distance = access
    for identifier, a, b in zip(_edge_ids(route), points, points[1:], strict=False):
        edge = graph.edges.get(identifier.rstrip("ab"))
        if edge is None:
            raise DomainError(503, "ev_graph_mismatch", "Route edge missing from the EV graph.")
        speed_kph = edge.speeds["car"] * 3.6
        speed_factor = 1 + 0.25 * (max(0, speed_kph - 90) / 90) ** 2
        weighted_distance += distance(a, b) * speed_factor
    if not _edge_ids(route):
        weighted_distance += metres
    nominal = weighted_distance / 100_000 * vehicle.consumption
    return {"lower_kwh": nominal * 0.85, "estimate_kwh": nominal, "upper_kwh": nominal * 1.3}


def _edge_ids(route: dict[str, object]) -> list[str]:
    return cast(list[str], route["edge_ids"])


def _boundary_allowed(graph: Graph, incoming: str, outgoing: str) -> bool:
    if not incoming or not outgoing:
        return True
    previous = graph.edges.get(incoming.rstrip("ab"))
    following = graph.edges.get(outgoing.rstrip("ab"))
    if previous is None or following is None:
        return False
    return previous.target != following.source or graph.allowed(previous.id, following, "car")


@dataclass(frozen=True)
class Leg:
    source: int
    target: int
    route: dict[str, object]
    energy: dict[str, float]
    departure: float
    arrival: float
    departure_upper: float
    arrival_upper: float
    charging: dict[str, object] | None


@dataclass(frozen=True)
class Label:
    location: int
    soc: float
    soc_upper: float
    incoming: str
    stops: int
    cost: float
    legs: tuple[Leg, ...] = ()


@dataclass
class RouteCache:
    dataset: OSMDataset
    points: list[Point]
    budget: Budget
    traffic: TrafficSnapshot | None
    now: datetime | None
    cache: dict[tuple[int, int], list[dict[str, object]]] = field(default_factory=dict)

    def routes(self, source: int, target: int) -> list[dict[str, object]]:
        key = (source, target)
        if key not in self.cache:
            paths: list[dict[str, object]] = []
            for strategy in ("fastest", "shortest"):
                try:
                    route = self.budget.route(
                        self.dataset,
                        self.points[source],
                        self.points[target],
                        strategy,
                        self.traffic,
                        self.now,
                    )
                except DomainError as exc:
                    if exc.code in {"route_not_found", "outside_coverage"}:
                        continue
                    raise
                if not any(_edge_ids(p) == _edge_ids(route) for p in paths):
                    paths.append(route)
            self.cache[key] = paths
        return self.cache[key]


def _search(
    graph: Graph,
    vehicle: Vehicle,
    stations: list[Station],
    routes: RouteCache,
    budget: Budget,
) -> Label:
    destination = len(routes.points) - 1
    serial = count()
    initial = Label(0, vehicle.initial, vehicle.initial, "", 0, 0)
    queue: list[tuple[float, int, Label]] = [(0, next(serial), initial)]
    frontier: dict[tuple[int, int, str, int], list[Label]] = {}
    frontier[(0, math.floor(vehicle.initial / budget.limits.soc_step + 1e-9), "", 0)] = [initial]
    step = budget.limits.soc_step
    while queue:
        budget.check()
        cost, _, label = heapq.heappop(queue)
        key = (label.location, math.floor(label.soc / step + 1e-9), label.incoming, label.stops)
        if label not in frontier.get(key, []):
            continue
        budget.expanded_states += 1
        if budget.expanded_states > budget.limits.state_cap:
            raise DomainError(503, "ev_budget_exceeded", "Shared EV charge-state cap reached.")
        if label.location == destination:
            return label
        charger = stations[label.location - 1] if label.location else None
        socket = charger.compatible_power(vehicle) if charger else None
        for target in range(1, destination + 1):
            if target == label.location:
                continue
            # Straight-line lower bound prunes only legs impossible even at a full battery.
            minimum = distance(routes.points[label.location], routes.points[target])
            if minimum / 100_000 * vehicle.consumption * 1.3 > (
                vehicle.capacity * (1 - vehicle.reserve)
            ):
                continue
            for route in routes.routes(label.location, target):
                budget.check()
                ids = _edge_ids(route)
                if ids and not _boundary_allowed(graph, label.incoming, ids[0]):
                    continue
                energy = energy_interval(route, vehicle, graph)
                required = vehicle.reserve + energy["upper_kwh"] / vehicle.capacity
                if required > 1 + 1e-9:
                    continue
                departures = {label.soc}
                if socket and label.stops < budget.limits.stop_cap:
                    departures.update(
                        min(1.0, index * step)
                        for index in range(1, math.ceil(1 / step) + 1)
                        if index * step > label.soc
                    )
                    departures.add(min(1.0, max(label.soc, required)))
                for departure in sorted(departures):
                    if departure + 1e-9 < required:
                        continue
                    charge: dict[str, object] | None = None
                    charge_upper = 0.0
                    stops = label.stops
                    if departure > label.soc + 1e-9:
                        if socket is None or charger is None:
                            continue
                        lower, charge_upper = charging_seconds(
                            vehicle,
                            socket[1],
                            label.soc,
                            departure,
                        )
                        lower, _ = charging_seconds(
                            vehicle,
                            socket[1],
                            min(departure, label.soc_upper),
                            departure,
                        )
                        charge = {
                            **charger.response(),
                            "connector": socket[0],
                            "rated_power_kw": socket[1],
                            "effective_power_limit_kw": min(socket[1], vehicle.maximum_power),
                            "arrival_soc_percent": label.soc * 100,
                            "departure_soc_percent": departure * 100,
                            "arrival_soc_interval_percent": {
                                "lower": label.soc * 100,
                                "upper": label.soc_upper * 100,
                            },
                            "departure_soc_interval_percent": {
                                "lower": departure * 100,
                                "upper": max(departure, label.soc_upper) * 100,
                            },
                            "energy_added_kwh": (departure - label.soc) * vehicle.capacity,
                            "energy_added_interval_kwh": {
                                "lower": max(0.0, departure - label.soc_upper) * vehicle.capacity,
                                "upper": (departure - label.soc) * vehicle.capacity,
                            },
                            "charging_s": charge_upper,
                            "charging_time_interval_s": {"lower": lower, "upper": charge_upper},
                            "curve_source": "user_calibrated"
                            if vehicle.curve
                            else "uncalibrated_estimate",
                        }
                        stops += 1
                    arrival = max(
                        vehicle.reserve, departure - energy["upper_kwh"] / vehicle.capacity
                    )
                    departure_upper = max(label.soc_upper, departure)
                    arrival_upper = max(
                        arrival,
                        departure_upper - energy["lower_kwh"] / vehicle.capacity,
                    )
                    access_s = (
                        cast(float, route["source_snap_m"])
                        + cast(float, route["destination_snap_m"])
                    ) / (5 / 3.6)
                    next_cost = cost + cast(float, route["eta_s"]) + access_s + charge_upper
                    next_label = Label(
                        target,
                        arrival,
                        arrival_upper,
                        ids[-1] if ids else label.incoming,
                        stops,
                        next_cost,
                        label.legs
                        + (
                            Leg(
                                label.location,
                                target,
                                route,
                                energy,
                                departure,
                                arrival,
                                departure_upper,
                                arrival_upper,
                                charge,
                            ),
                        ),
                    )
                    next_key = (
                        target,
                        math.floor(arrival / step + 1e-9),
                        next_label.incoming,
                        stops,
                    )
                    existing = frontier.get(next_key, [])
                    if any(p.cost <= next_cost and p.soc >= arrival for p in existing):
                        continue
                    frontier[next_key] = [
                        p for p in existing if not (next_cost <= p.cost and arrival >= p.soc)
                    ] + [next_label]
                    if len(queue) >= budget.limits.state_cap:
                        raise DomainError(
                            503,
                            "ev_budget_exceeded",
                            "Shared EV queued-state cap reached.",
                        )
                    heapq.heappush(queue, (next_cost, next(serial), next_label))
    raise DomainError(
        422,
        "ev_trip_unreachable",
        "No reserve-safe EV itinerary in the bounded imported charger/road candidates.",
    )


def _provenance(dataset: OSMDataset) -> dict[str, object]:
    return {
        "source": "OpenStreetMap",
        "graph_version": dataset.checksum,
        "license": "ODbL 1.0; © OpenStreetMap contributors",
        "coverage": "active_local_osm_dataset",
    }


def plan_ev_trip(
    dataset: OSMDataset,
    source: Point,
    destination: Point,
    vehicle_request: VehicleRequest | None = None,
    traffic: TrafficSnapshot | None = None,
    now: datetime | None = None,
    limits: PlanningLimits = PlanningLimits(),
) -> dict[str, object]:
    """Top-level pickleable CPU entry point; no I/O, repository, or async objects."""
    budget = Budget(limits, time.monotonic() + limits.wall_seconds)
    for point in (source, destination):
        if (
            len(point) != 2
            or not all(math.isfinite(value) for value in point)
            or not -180 <= point[0] <= 180
            or not -90 <= point[1] <= 90
        ):
            raise DomainError(422, "invalid_coordinate", "Coordinates must be finite WGS84.")
    ordinary = budget.route(dataset, source, destination, "fastest", traffic, now)
    missing = vehicle_request.missing() if vehicle_request else list(REQUIRED)
    if missing:
        return {
            "ev_planning_available": False,
            "feasible": None,
            "fallback_reason": "missing_vehicle_parameters",
            "missing_vehicle_parameters": missing,
            "ordinary_route": ordinary,
            "legs": [],
            "charging_stops": [],
            "provenance": _provenance(dataset),
            "warnings": ["EV feasibility was not assessed; vehicle parameters are missing."],
        }
    vehicle = Vehicle.from_request(cast(VehicleRequest, vehicle_request))
    previous = sys.gettrace()
    sys.settrace(budget.trace)
    try:
        graph = build_graph(dataset)
        apply_snapshot(graph, traffic or TrafficSnapshot(), now or datetime.now(UTC))
        catalog, excluded = charging_stations(dataset, graph, budget)
        compatible: list[Station] = []
        for station in catalog:
            if station.compatible_power(vehicle):
                compatible.append(station)
            else:
                excluded.append(
                    {"source_id": station.source_id, "reason": "incompatible_connectors"}
                )
        compatible.sort(
            key=lambda s: (
                distance(source, s.point) + distance(s.point, destination),
                s.source_id,
            )
        )
        stations = compatible[: limits.candidate_cap]
        truncated = len(compatible) > len(stations)
        points = [source, *(station.point for station in stations), destination]
        cache = RouteCache(dataset, points, budget, traffic, now)
        cache.cache[0, len(points) - 1] = [ordinary]
        # Also consider a shorter, slower ordinary leg for energy feasibility.
        shortest = budget.route(dataset, source, destination, "shortest", traffic, now)
        if _edge_ids(shortest) != _edge_ids(ordinary):
            cache.cache[0, len(points) - 1].append(shortest)
        result = _search(graph, vehicle, stations, cache, budget)
    finally:
        sys.settrace(previous)
    legs: list[dict[str, object]] = []
    stops: list[dict[str, object]] = []
    lower_energy = nominal_energy = upper_energy = driving = access_seconds = 0.0
    charging_lower = charging_upper = 0.0
    for index, leg in enumerate(result.legs):
        if leg.charging:
            stops.append({**leg.charging, "before_leg": index})
            interval = cast(dict[str, float], leg.charging["charging_time_interval_s"])
            charging_lower += interval["lower"]
            charging_upper += interval["upper"]
        lower_energy += leg.energy["lower_kwh"]
        nominal_energy += leg.energy["estimate_kwh"]
        upper_energy += leg.energy["upper_kwh"]
        driving += cast(float, leg.route["eta_s"])
        access_seconds += (
            cast(float, leg.route["source_snap_m"]) + cast(float, leg.route["destination_snap_m"])
        ) / (5 / 3.6)
        legs.append(
            {
                "index": index,
                "source": {
                    "latitude": points[leg.source][1],
                    "longitude": points[leg.source][0],
                },
                "destination": {
                    "latitude": points[leg.target][1],
                    "longitude": points[leg.target][0],
                },
                "route": leg.route,
                "energy": leg.energy,
                "departure_soc_percent": leg.departure * 100,
                "arrival_soc_percent": leg.arrival * 100,
                "arrival_soc_interval_percent": {
                    "lower": leg.arrival * 100,
                    "upper": leg.arrival_upper * 100,
                },
                "departure_soc_interval_percent": {
                    "lower": leg.departure * 100,
                    "upper": leg.departure_upper * 100,
                },
                "reserve_percent": vehicle.reserve * 100,
                "minimum_soc_percent": leg.arrival * 100,
            }
        )
    warnings = [
        *cast(list[str], ordinary["warnings"]),
        "Elevation and weather are unavailable; energy interval uses declared 0.85–1.30 factors.",
        "Consumption is a user parameter, not a calibrated guarantee; no regeneration credited.",
        "Every leg spends upper-bound energy with reserve; SOC lower bounds guide charging.",
        "Charger availability, queues, opening hours and delivered power are unverified.",
        "Charging times exclude queue/plug-in time and cannot guarantee station operation.",
        "Optimization is regional and bounded to SOC targets and fastest/shortest pairwise paths.",
    ]
    if vehicle.curve is None:
        warnings.append(
            "No calibrated charging curve: explicit estimated taper and time interval used."
        )
    if truncated:
        warnings.append(
            "Compatible charger candidate cap truncated the catalog; no global optimum claimed."
        )
    budget.check()
    return {
        "ev_planning_available": True,
        "feasible": True,
        "fallback_reason": None,
        "ordinary_route": None,
        "legs": legs,
        "charging_stops": stops,
        "initial_soc_percent": vehicle.initial * 100,
        "destination_soc_percent": result.soc * 100,
        "destination_soc_interval_percent": {
            "lower": result.soc * 100,
            "upper": result.soc_upper * 100,
        },
        "reserve_percent": vehicle.reserve * 100,
        "energy": {
            "lower_kwh": lower_energy,
            "estimate_kwh": nominal_energy,
            "upper_kwh": upper_energy,
        },
        "energy_model": {
            "source": "user_per_edge_distance_consumption_with_effective_speed_penalty",
            "calibrated": False,
            "elevation_available": False,
            "weather_available": False,
            "lower_factor": 0.85,
            "upper_factor": 1.3,
        },
        "driving_s": driving,
        "detour_s": max(0.0, driving - cast(float, ordinary["eta_s"])),
        "access_s": access_seconds,
        "charging_s": charging_upper,
        "charging_time_interval_s": {"lower": charging_lower, "upper": charging_upper},
        "total_time_s": driving + access_seconds + charging_upper,
        "time_components": {
            "direct_driving_s": cast(float, ordinary["eta_s"]),
            "itinerary_driving_s": driving,
            "additional_detour_s": max(0.0, driving - cast(float, ordinary["eta_s"])),
            "access_s": access_seconds,
            "charging_s": charging_upper,
            "queue_s": None,
            "note": "detour is included in itinerary driving; do not add it twice",
        },
        "provenance": _provenance(dataset),
        "excluded_chargers": excluded,
        "search": {
            "algorithm": "bounded_charge_state_dijkstra",
            "objective": "itinerary_driving_plus_access_plus_upper_charging_seconds",
            "soc_step_percent": limits.soc_step * 100,
            "candidate_count": len(stations),
            "candidate_cap": limits.candidate_cap,
            "candidates_truncated": truncated,
            "route_calls": budget.route_calls,
            "route_call_cap": limits.route_call_cap,
            "expanded_states": budget.expanded_states,
            "state_cap": limits.state_cap,
            "stop_cap": limits.stop_cap,
            "wall_deadline_s": limits.wall_seconds,
        },
        "warnings": warnings,
    }
