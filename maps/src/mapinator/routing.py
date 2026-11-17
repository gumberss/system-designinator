import heapq
import math
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import count
from typing import Literal, TypedDict

from mapinator.domain import DomainError
from mapinator.osm import OSMDataset
from mapinator.traffic import TrafficSnapshot, TrafficSource, apply_snapshot

Mode = Literal["car", "bicycle", "walking"]
Strategy = Literal["fastest", "shortest"]
Point = tuple[float, float]  # GeoJSON longitude, latitude
Cell = tuple[int, int]
State = tuple[int, str]
Transition = tuple[State, tuple[str, ...], float]
ShortcutKey = tuple[State, Mode, Strategy, int, bool, bool, frozenset[str]]


class InstructionData(TypedDict):
    action: str
    name: str
    distance_m: float
    coordinate: Point


CAR_ROADS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "service",
    "living_street",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
}
SPEEDS = {
    "motorway": 100,
    "trunk": 80,
    "primary": 60,
    "secondary": 50,
    "tertiary": 40,
    "residential": 30,
    "service": 15,
    "living_street": 10,
}
MODES: tuple[Mode, ...] = ("car", "bicycle", "walking")


def distance(a: Point, b: Point) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(math.sqrt(min(1, h)))


@dataclass(frozen=True)
class Edge:
    id: str
    source: int
    target: int
    way: int
    direction: int
    length: float
    speeds: dict[Mode, float]
    segment: Cell
    name: str
    toll: bool
    motorway: bool

    def cost(self, mode: Mode, strategy: Strategy) -> float:
        return self.length if strategy == "shortest" else self.length / self.speeds[mode]


@dataclass(frozen=True)
class Restriction:
    source_way: int
    via: int
    target_way: int
    kind: str
    modes: frozenset[Mode]


@dataclass
class Graph:
    nodes: dict[int, Point]
    edges: dict[str, Edge]
    restrictions: list[Restriction]
    warnings: list[str]
    cell_size: float
    outgoing: dict[int, list[Edge]] = field(default_factory=dict)
    portals: set[int] = field(default_factory=set)
    segments: dict[Cell, set[int]] = field(default_factory=dict)
    shortcuts: dict[ShortcutKey, list[Transition]] = field(default_factory=dict)

    def index(self) -> None:
        self.shortcuts.clear()
        self.outgoing = {}
        self.segments = {}
        membership: dict[int, set[Cell]] = {}
        for edge in self.edges.values():
            self.outgoing.setdefault(edge.source, []).append(edge)
            self.segments.setdefault(edge.segment, set()).update((edge.source, edge.target))
            membership.setdefault(edge.source, set()).add(edge.segment)
            membership.setdefault(edge.target, set()).add(edge.segment)
        self.portals = {node for node, cells in membership.items() if len(cells) > 1}

    def allowed(self, previous: str, edge: Edge, mode: Mode) -> bool:
        if not previous:
            return True
        incoming = self.edges[previous]
        for rule in self.restrictions:
            if mode not in rule.modes or rule.via != edge.source or rule.source_way != incoming.way:
                continue
            matches = edge.way == rule.target_way
            if rule.kind in {"no_u_turn", "only_u_turn"}:
                matches = matches and edge.direction != incoming.direction
            elif rule.kind == "only_straight_on" and incoming.way == edge.way:
                matches = matches and edge.direction == incoming.direction
            if (rule.kind.startswith("only_") and not matches) or (
                rule.kind.startswith("no_") and matches
            ):
                return False
        return True


def _permissions(tags: dict[str, str], warnings: set[str]) -> dict[Mode, float]:
    highway = tags.get("highway", "")
    if highway in {"", "construction", "proposed", "raceway"} or tags.get("route") == "ferry":
        return {}
    if any(key.endswith(":conditional") for key in tags):
        warnings.add("Conditional access/restrictions excluded conservatively.")
        return {}
    result: dict[Mode, float] = {}
    for mode in MODES:
        keys = {
            "car": ("access", "vehicle", "motor_vehicle", "motorcar"),
            "bicycle": ("access", "vehicle", "bicycle"),
            "walking": ("access", "foot"),
        }[mode]
        access = next((tags[key] for key in reversed(keys) if key in tags), None)
        default = (
            highway in CAR_ROADS
            if mode == "car"
            else highway not in {"motorway", "motorway_link", "trunk", "trunk_link"}
        )
        if access in {"no", "private", "destination", "customers", "delivery", "agricultural"}:
            continue
        if access not in {None, "yes", "designated", "permissive"}:
            warnings.add("Unknown access values excluded conservatively.")
            continue
        override = any(tags.get(key) in {"yes", "designated", "permissive"} for key in keys[1:])
        if not default and not override:
            continue
        if mode == "bicycle" and highway == "steps" and tags.get("bicycle") != "yes":
            continue
        speed = (
            5.0
            if mode == "walking"
            else 18.0
            if mode == "bicycle"
            else SPEEDS.get(highway.removesuffix("_link"), 30)
        )
        if mode == "car" and "maxspeed" in tags:
            raw = tags["maxspeed"].lower()
            try:
                limit = float(raw.replace("mph", "").replace("km/h", "").strip())
                if "mph" in raw:
                    limit *= 1.609344
                if math.isfinite(limit) and limit > 0:
                    speed = min(speed, limit)
                else:
                    warnings.add("Unknown maxspeed: baseline speed used.")
            except ValueError:
                warnings.add("Unknown maxspeed: baseline speed used.")
        if tags.get("surface") in {"gravel", "unpaved", "sand", "ground", "cobblestone"}:
            speed *= 0.7
        result[mode] = speed / 3.6
    return result


def build_graph(dataset: OSMDataset, cell_size: float = 0.01) -> Graph:
    if not math.isfinite(cell_size) or cell_size <= 0:
        raise ValueError("cell_size must be finite and positive.")
    nodes = {n.osm_id: (n.longitude, n.latitude) for n in dataset.nodes}
    warnings: set[str] = {
        "ETA uses baseline speeds; no real-time traffic.",
        "Regional experimental routing; not certified for real navigation.",
    }
    blocked: set[int] = set()
    restrictions: list[Restriction] = []
    for relation in dataset.relations:
        if relation.tags.get("type") != "restriction":
            continue
        members = relation.members
        from_ids = [ref for kind, ref, role in members if kind == "way" and role == "from"]
        to_ids = [ref for kind, ref, role in members if kind == "way" and role == "to"]
        via = [ref for kind, ref, role in members if kind == "node" and role == "via"]
        kinds = {k: v for k, v in relation.tags.items() if k.startswith("restriction")}
        if (
            len(from_ids) != 1
            or len(to_ids) != 1
            or len(via) != 1
            or len([m for m in members if m[2] == "via"]) != 1
            or any("conditional" in key for key in kinds)
            or not kinds
        ):
            blocked.update(from_ids + to_ids)
            warnings.add("Unsupported/incomplete turn restriction: affected ways excluded.")
            continue
        except_modes = set(relation.tags.get("except", "").split(";"))
        for key, kind in kinds.items():
            if kind not in {
                "no_right_turn",
                "no_left_turn",
                "no_straight_on",
                "no_u_turn",
                "no_entry",
                "no_exit",
                "only_right_turn",
                "only_left_turn",
                "only_straight_on",
                "only_u_turn",
            }:
                blocked.update(from_ids + to_ids)
                warnings.add("Unknown turn restriction: affected ways excluded.")
                continue
            rule_modes = {
                mode
                for mode in MODES
                if (key == "restriction" and mode != "walking")
                or key
                == {
                    "car": "restriction:motorcar",
                    "bicycle": "restriction:bicycle",
                    "walking": "restriction:foot",
                }[mode]
            }
            rule_modes = {
                m
                for m in rule_modes
                if {"car": "motorcar", "bicycle": "bicycle", "walking": "foot"}[m]
                not in except_modes
            }
            if not rule_modes:
                blocked.update(from_ids + to_ids)
                warnings.add("Unsupported restriction mode: affected ways excluded.")
            else:
                restrictions.append(
                    Restriction(from_ids[0], via[0], to_ids[0], kind, frozenset(rule_modes))
                )
    barriers = {
        n.osm_id
        for n in dataset.nodes
        if n.tags.get("barrier") not in {None, "no", "entrance", "kerb"}
    }
    edges: dict[str, Edge] = {}
    virtual = -1
    for way in dataset.ways:
        if way.osm_id in blocked:
            continue
        speeds = _permissions(way.tags, warnings)
        if not speeds:
            continue
        if any(ref in barriers for ref in way.refs):
            warnings.add("Ways containing barriers excluded conservatively.")
            continue
        for pair, (u, v) in enumerate(zip(way.refs, way.refs[1:], strict=False)):
            a, b = nodes[u], nodes[v]
            if distance(a, b) < 0.01:
                continue
            fractions = {0.0, 1.0}
            for axis in (0, 1):
                if a[axis] == b[axis]:
                    continue
                lo, hi = sorted((a[axis], b[axis]))
                for grid in range(math.floor(lo / cell_size) + 1, math.ceil(hi / cell_size)):
                    fractions.add((grid * cell_size - a[axis]) / (b[axis] - a[axis]))
            chain = [u]
            for f in sorted(fractions)[1:-1]:
                nodes[virtual] = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
                chain.append(virtual)
                virtual -= 1
            chain.append(v)
            for piece, (s, t) in enumerate(zip(chain, chain[1:], strict=False)):
                mid = ((nodes[s][0] + nodes[t][0]) / 2, (nodes[s][1] + nodes[t][1]) / 2)
                cell = (math.floor(mid[0] / cell_size), math.floor(mid[1] / cell_size))
                for direction in (1, -1):
                    allowed: dict[Mode, float] = {}
                    for mode, speed in speeds.items():
                        one = way.tags.get(
                            "oneway", "yes" if way.tags.get("junction") == "roundabout" else "no"
                        )
                        if mode == "walking":
                            one = way.tags.get("oneway:foot", "no")
                        elif mode == "bicycle":
                            one = way.tags.get("oneway:bicycle", one)
                        elif "oneway:motorcar" in way.tags:
                            one = way.tags["oneway:motorcar"]
                        if one not in {"yes", "1", "true", "-1", "no", "0", "false"}:
                            warnings.add("Unknown oneway excluded conservatively.")
                            continue
                        if (one in {"yes", "1", "true"} and direction == -1) or (
                            one == "-1" and direction == 1
                        ):
                            continue
                        allowed[mode] = speed
                    if allowed:
                        identifier = f"{way.osm_id}:{pair}:{piece}:{direction}"
                        edges[identifier] = Edge(
                            identifier,
                            s if direction == 1 else t,
                            t if direction == 1 else s,
                            way.osm_id,
                            direction,
                            distance(nodes[s], nodes[t]),
                            allowed,
                            cell,
                            way.tags.get("name", ""),
                            way.tags.get("toll") == "yes",
                            way.tags.get("highway", "").startswith("motorway"),
                        )
    graph = Graph(nodes, edges, restrictions, sorted(warnings), cell_size)
    graph.index()
    return graph


def snap(
    graph: Graph, point: Point, mode: Mode, avoid_tolls: bool, avoid_motorways: bool
) -> tuple[int, float]:
    best: tuple[float, Edge, float, Point] | None = None
    for edge in graph.edges.values():
        if (
            mode not in edge.speeds
            or (avoid_tolls and edge.toll)
            or (avoid_motorways and edge.motorway)
        ):
            continue
        a, b = graph.nodes[edge.source], graph.nodes[edge.target]
        scale = math.cos(math.radians(point[1]))
        dx, dy = (b[0] - a[0]) * scale, b[1] - a[1]
        px, py = (point[0] - a[0]) * scale, point[1] - a[1]
        f = (
            max(0.0, min(1.0, (px * dx + py * dy) / (dx * dx + dy * dy)))
            if dx * dx + dy * dy
            else 0.0
        )
        projected = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
        error = distance(point, projected)
        if best is None or error < best[0]:
            best = (error, edge, f, projected)
    if best is None or best[0] > 100:
        raise DomainError(422, "outside_coverage", "No permitted road within 100 metres.")
    error, selected, fraction, projected = best
    if fraction < 1e-9:
        return selected.source, error
    if fraction > 1 - 1e-9:
        return selected.target, error
    vertex = min(0, *graph.nodes) - 1
    graph.nodes[vertex] = projected
    siblings = [
        e
        for e in graph.edges.values()
        if e.way == selected.way and {e.source, e.target} == {selected.source, selected.target}
    ]
    for edge in siblings:
        del graph.edges[edge.id]
        for suffix, start, end in (("a", edge.source, vertex), ("b", vertex, edge.target)):
            identifier = edge.id + suffix
            graph.edges[identifier] = replace(
                edge,
                id=identifier,
                source=start,
                target=end,
                length=distance(graph.nodes[start], graph.nodes[end]),
            )
    graph.index()
    return vertex, error


@dataclass(frozen=True)
class PathResult:
    edges: tuple[str, ...]
    cost: float
    expanded: int
    shortcut_count: int


def pathfind(
    graph: Graph,
    start: int,
    goal: int,
    mode: Mode,
    strategy: Strategy,
    algorithm: Literal["astar", "dijkstra", "hierarchical"] = "hierarchical",
    avoid_tolls: bool = False,
    avoid_motorways: bool = False,
    forbidden: frozenset[str] = frozenset(),
    max_states: int = 200_000,
    deadline: float | None = None,
) -> PathResult:
    started = time.monotonic()
    deadline = min(deadline, started + 3) if deadline is not None else started + 3
    visits = 0
    shortcuts = 0
    max_speed = max((e.speeds.get(mode, 0) for e in graph.edges.values()), default=1) or 1

    def heuristic(node: int) -> float:
        if algorithm == "dijkstra":
            return 0
        metres = distance(graph.nodes[node], graph.nodes[goal])
        return metres if strategy == "shortest" else metres / max_speed

    def transitions(state: State) -> list[Transition]:
        nonlocal visits, shortcuts

        def eligible(current: State) -> list[Edge]:
            return [
                e
                for e in graph.outgoing.get(current[0], [])
                if mode in e.speeds
                and e.id not in forbidden
                and not (avoid_tolls and e.toll)
                and not (avoid_motorways and e.motorway)
                and graph.allowed(current[1], e, mode)
            ]

        if algorithm != "hierarchical":
            return [((e.target, e.id), (e.id,), e.cost(mode, strategy)) for e in eligible(state)]
        key = (state, mode, strategy, goal, avoid_tolls, avoid_motorways, forbidden)
        if key in graph.shortcuts:
            result = graph.shortcuts[key]
            shortcuts += len(result)
            return result
        result = []
        # Edge-based local Dijkstra preserves turn restrictions at portal boundaries.
        for cell in sorted({e.segment for e in eligible(state)}):
            local: list[tuple[float, State, tuple[str, ...]]] = [(0, state, ())]
            costs = {state: 0.0}
            while local:
                cost, current, path = heapq.heappop(local)
                if cost != costs[current]:
                    continue
                budget()
                if path and (current[0] in graph.portals or current[0] == goal):
                    result.append((current, path, cost))
                    shortcuts += 1
                    continue
                for edge in eligible(current):
                    if edge.segment != cell:
                        continue
                    next_state = (edge.target, edge.id)
                    candidate = cost + edge.cost(mode, strategy)
                    if candidate < costs.get(next_state, math.inf):
                        costs[next_state] = candidate
                        heapq.heappush(local, (candidate, next_state, path + (edge.id,)))
        if len(graph.shortcuts) < 4096:
            graph.shortcuts[key] = result
        return result

    def budget() -> None:
        nonlocal visits
        visits += 1
        if visits > max_states or time.monotonic() > deadline:
            raise DomainError(
                503, "routing_budget_exceeded", "Regional routing resource limit reached."
            )

    initial = (start, "")
    serial = count()
    queue: list[tuple[float, int, float, State]] = [(heuristic(start), next(serial), 0, initial)]
    costs = {initial: 0.0}
    parents: dict[State, tuple[State, tuple[str, ...]]] = {}
    while queue:
        _, _, cost, state = heapq.heappop(queue)
        if costs[state] != cost:
            continue
        budget()
        if state[0] == goal:
            chunks = []
            while state != initial:
                state, path = parents[state]
                chunks.append(path)
            return PathResult(
                tuple(e for p in reversed(chunks) for e in p), cost, visits, shortcuts
            )
        for target, path, weight in transitions(state):
            candidate = cost + weight
            if candidate < costs.get(target, math.inf):
                costs[target] = candidate
                parents[target] = (state, path)
                heapq.heappush(
                    queue, (candidate + heuristic(target[0]), next(serial), candidate, target)
                )
    raise DomainError(
        422, "route_not_found", "No permitted route within the imported road network."
    )


def calculate(
    dataset: OSMDataset,
    source: Point,
    destination: Point,
    mode: Mode,
    strategy: Strategy,
    alternatives: int = 0,
    avoid_tolls: bool = False,
    avoid_motorways: bool = False,
    traffic: TrafficSnapshot | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    graph = build_graph(dataset)
    snapshot = traffic or TrafficSnapshot()
    sources = apply_snapshot(graph, snapshot, now or datetime.now(UTC))
    deadline = time.monotonic() + 3
    start, source_error = snap(graph, source, mode, avoid_tolls, avoid_motorways)
    goal, destination_error = snap(graph, destination, mode, avoid_tolls, avoid_motorways)
    primary = pathfind(
        graph,
        start,
        goal,
        mode,
        strategy,
        avoid_tolls=avoid_tolls,
        avoid_motorways=avoid_motorways,
        deadline=deadline,
    )
    paths = [primary]
    if alternatives:
        for edge in primary.edges[:30]:
            try:
                candidate = pathfind(
                    graph,
                    start,
                    goal,
                    mode,
                    strategy,
                    avoid_tolls=avoid_tolls,
                    avoid_motorways=avoid_motorways,
                    forbidden=frozenset({edge}),
                    deadline=deadline,
                )
            except DomainError as exc:
                if exc.code == "route_not_found":
                    continue
                raise
            if candidate.edges not in [p.edges for p in paths]:
                overlap = sum(graph.edges[e].length for e in candidate.edges if e in primary.edges)
                length = sum(graph.edges[e].length for e in candidate.edges)
                if length and overlap / length <= 0.85:
                    paths.append(candidate)
            if len(paths) > alternatives:
                break
    paths[1:] = sorted(paths[1:], key=lambda p: p.cost)

    def render(path: PathResult) -> dict[str, object]:
        chosen = [graph.edges[e] for e in path.edges]
        points = [graph.nodes[start]] + [graph.nodes[e.target] for e in chosen]
        if len(points) == 1:
            points.append(points[0])
        instructions: list[InstructionData] = []
        for index, edge in enumerate(chosen):
            action = "depart" if index == 0 else "continue"
            if index and chosen[index - 1].way != edge.way:
                a, b, c = (
                    graph.nodes[chosen[index - 1].source],
                    graph.nodes[edge.source],
                    graph.nodes[edge.target],
                )
                incoming = math.atan2(b[1] - a[1], (b[0] - a[0]) * math.cos(math.radians(b[1])))
                outgoing = math.atan2(c[1] - b[1], (c[0] - b[0]) * math.cos(math.radians(b[1])))
                angle = (outgoing - incoming + math.pi) % (2 * math.pi) - math.pi
                action = (
                    "turn_left" if angle > 0.45 else "turn_right" if angle < -0.45 else "continue"
                )
            if instructions and action == "continue" and instructions[-1]["name"] == edge.name:
                instructions[-1]["distance_m"] += edge.length
            else:
                instructions.append(
                    {
                        "action": action,
                        "name": edge.name,
                        "distance_m": edge.length,
                        "coordinate": graph.nodes[edge.source],
                    }
                )
        instructions.append(
            {"action": "arrive", "name": "", "distance_m": 0, "coordinate": graph.nodes[goal]}
        )
        kinds: set[TrafficSource] = {
            sources.get(e.id.rstrip("ab"), "baseline") if mode == "car" else "baseline"
            for e in chosen
        }
        source: TrafficSource = (
            next(iter(kinds)) if len(kinds) == 1 else "mixed" if kinds else "baseline"
        )
        return {
            "traffic_source": source,
            "traffic_version": snapshot.version,
            "segment_ids": sorted({f"{e.segment[0]}:{e.segment[1]}" for e in chosen}),
            "geometry": {"type": "LineString", "coordinates": points},
            "edge_ids": list(path.edges),
            "distance_m": sum(e.length for e in chosen),
            "eta_s": sum(e.length / e.speeds[mode] for e in chosen),
            "edge_durations_s": [e.length / e.speeds[mode] for e in chosen],
            "instructions": instructions,
            "expanded_states": path.expanded,
            "shortcuts_resolved": path.shortcut_count,
        }

    warnings = graph.warnings[:]
    if any(source != "baseline" for source in sources.values()) and mode == "car":
        warnings = [w for w in warnings if w != "ETA uses baseline speeds; no real-time traffic."]
        warnings.append(
            "ETA combines traffic confidence/freshness with baseline or historical estimates."
        )
    if snapshot.unavailable:
        warnings.append("Traffic storage unavailable; routing explicitly uses baseline weights.")
    if len(paths) - 1 < alternatives:
        warnings.append("Requested number of diverse alternatives not available in bounded search.")
    return {
        **render(primary),
        "alternatives": [render(p) for p in paths[1:]],
        "mode": mode,
        "strategy": strategy,
        "preferences": {"avoid_tolls": avoid_tolls, "avoid_motorways": avoid_motorways},
        "algorithm": "hierarchical",
        "graph_version": dataset.checksum,
        "source_snap_m": source_error,
        "destination_snap_m": destination_error,
        "segment_count": len(graph.segments),
        "portal_count": len(graph.portals),
        "warnings": warnings,
    }
