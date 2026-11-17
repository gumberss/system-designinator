import json
import pickle
import random
from pathlib import Path
from typing import Literal

import pytest

from mapinator.domain import DomainError
from mapinator.osm import OSMDataset, OSMNode, OSMRelation, OSMWay, parse_osm
from mapinator.routing import build_graph, calculate, distance, pathfind, snap


def network(
    coordinates: list[tuple[float, float]],
    connections: list[tuple[int, int, dict[str, str]]],
    relations: tuple[OSMRelation, ...] = (),
) -> OSMDataset:
    return OSMDataset(
        "synthetic",
        tuple(OSMNode(i + 1, lat, lon, {}) for i, (lon, lat) in enumerate(coordinates)),
        tuple(
            OSMWay(10 + i, (u, v), {"highway": "residential", **tags})
            for i, (u, v, tags) in enumerate(connections)
        ),
        (),
        (0, 0, 1, 1),
        relations,
    )


def test_haversine_is_metres() -> None:
    assert distance((0, 0), (0, 0.001)) == pytest.approx(111.195, abs=0.01)


@pytest.mark.parametrize("algorithm", ["astar", "dijkstra", "hierarchical"])
def test_fastest_and_shortest_are_different(
    algorithm: Literal["astar", "dijkstra", "hierarchical"],
) -> None:
    data = network(
        [(0, 0), (0.002, 0), (0.001, 0.001)],
        [
            (1, 2, {"highway": "service"}),
            (1, 3, {"highway": "primary"}),
            (3, 2, {"highway": "primary"}),
        ],
    )
    graph = build_graph(data, cell_size=0.001)
    shortest = pathfind(graph, 1, 2, "car", "shortest", algorithm)
    fastest = pathfind(graph, 1, 2, "car", "fastest", algorithm)
    assert {graph.edges[e].way for e in shortest.edges} == {10}
    assert {graph.edges[e].way for e in fastest.edges} == {11, 12}


def test_direction_modes_preferences_and_fractional_snap() -> None:
    data = network(
        [(0, 0), (0.004, 0)],
        [
            (1, 2, {"oneway": "yes", "oneway:bicycle": "no", "toll": "yes"}),
        ],
    )
    result = calculate(data, (0.001, 0), (0.003, 0), "car", "shortest")
    assert float(str(result["distance_m"])) == pytest.approx(distance((0.001, 0), (0.003, 0)))
    geometry = result["geometry"]
    assert isinstance(geometry, dict) and geometry["coordinates"][0] == pytest.approx((0.001, 0))
    with pytest.raises(DomainError, match="No permitted route"):
        calculate(data, (0.003, 0), (0.001, 0), "car", "fastest")
    assert calculate(data, (0.003, 0), (0.001, 0), "walking", "shortest")["distance_m"]
    assert calculate(data, (0.003, 0), (0.001, 0), "bicycle", "shortest")["distance_m"]
    with pytest.raises(DomainError, match="100 metres"):
        calculate(data, (0.001, 0), (0.003, 0), "car", "fastest", avoid_tolls=True)


def test_turn_restriction_across_segment_boundary() -> None:
    rule = OSMRelation(
        1,
        (("way", 10, "from"), ("node", 2, "via"), ("way", 11, "to")),
        {"type": "restriction", "restriction": "no_right_turn"},
    )
    data = network(
        [(0, 0), (0.002, 0), (0.002, -0.002), (0.004, 0), (0.004, -0.002)],
        [(1, 2, {}), (2, 3, {}), (2, 4, {}), (4, 5, {}), (5, 3, {})],
        (rule,),
    )
    graph = build_graph(data, 0.001)
    for mode in ("car", "bicycle"):
        for algorithm in ("astar", "hierarchical"):
            route = pathfind(
                graph,
                1,
                3,
                mode,
                "shortest",
                algorithm,
            )
            assert len(route.edges) > 2
            assert route.cost == pytest.approx(
                pathfind(graph, 1, 3, mode, "shortest", "dijkstra").cost
            )
            for left, right in zip(route.edges, route.edges[1:], strict=False):
                assert graph.allowed(left, graph.edges[right], mode)
    walking = pathfind(graph, 1, 3, "walking", "shortest")
    assert {graph.edges[e].way for e in walking.edges} == {10, 11}


def test_unsupported_via_way_is_not_silently_ignored() -> None:
    rule = OSMRelation(
        1,
        (("way", 10, "from"), ("way", 11, "via"), ("way", 12, "to")),
        {"type": "restriction", "restriction": "no_straight_on"},
    )
    data = network(
        [(0, 0), (0.001, 0), (0.002, 0), (0.003, 0)], [(1, 2, {}), (2, 3, {}), (3, 4, {})], (rule,)
    )
    graph = build_graph(data)
    assert {e.way for e in graph.edges.values()} == {11}
    assert any("affected ways excluded" in warning for warning in graph.warnings)


def test_portals_split_roads_without_boundary_intersections() -> None:
    graph = build_graph(network([(0.0001, 0.0001), (0.0039, 0.0001)], [(1, 2, {})]), 0.001)
    assert len(graph.segments) == 4
    assert len(graph.portals) == 3
    result = pathfind(graph, 1, 2, "car", "shortest")
    assert result.shortcut_count >= 3
    assert result.cost == pytest.approx(pathfind(graph, 1, 2, "car", "shortest", "dijkstra").cost)


def test_route_can_leave_source_destination_bounding_box() -> None:
    graph = build_graph(
        network(
            [(0, 0), (0.001, 0), (0, 0.004), (0.001, 0.004)], [(1, 3, {}), (3, 4, {}), (4, 2, {})]
        ),
        0.001,
    )
    route = pathfind(graph, 1, 2, "car", "shortest")
    assert any(graph.nodes[graph.edges[e].target][1] > 0.003 for e in route.edges)
    assert route.cost == pytest.approx(pathfind(graph, 1, 2, "car", "shortest", "dijkstra").cost)


def test_limits_unreachable_and_private_access() -> None:
    graph = build_graph(network([(0, 0), (0.001, 0)], [(1, 2, {"access": "private"})]))
    with pytest.raises(DomainError, match="100 metres"):
        snap(graph, (0, 0), "car", False, False)
    graph = build_graph(network([(0, 0), (0.001, 0)], [(1, 2, {})]))
    with pytest.raises(DomainError, match="resource limit"):
        pathfind(graph, 1, 2, "car", "shortest", max_states=1)
    exc = DomainError(422, "test", "No permitted route")
    restored = pickle.loads(pickle.dumps(exc))
    assert restored.status == 422 and restored.code == "test"


@pytest.mark.parametrize("size", [4, 10, 25])
def test_random_grid_oracle(size: int) -> None:
    randomizer = random.Random(size)
    points = [(x * 0.0011, y * 0.0011) for y in range(size) for x in range(size)]
    connections = []
    for y in range(size):
        for x in range(size):
            u = y * size + x + 1
            for v in (u + 1 if x + 1 < size else None, u + size if y + 1 < size else None):
                if v:
                    connections.append((u, v, {"maxspeed": str(randomizer.choice([10, 30, 50]))}))
    graph = build_graph(network(points, connections), 0.004)
    for _ in range(4):
        start, goal = randomizer.sample(range(1, size * size + 1), 2)
        for strategy in ("fastest", "shortest"):
            oracle = pathfind(graph, start, goal, "car", strategy, "dijkstra")
            for algorithm in ("astar", "hierarchical"):
                result = pathfind(
                    graph,
                    start,
                    goal,
                    "car",
                    strategy,
                    algorithm,
                )
                assert result.cost == pytest.approx(oracle.cost)


def test_alternatives_geometry_instructions() -> None:
    data = network(
        [(0, 0), (0.002, 0), (0.001, 0.001)],
        [(1, 2, {}), (1, 3, {"name": "North"}), (3, 2, {"name": "East"})],
    )
    result = calculate(data, (0, 0), (0.002, 0), "car", "shortest", alternatives=1)
    assert result["alternatives"]
    assert result["traffic_source"] == "baseline"
    assert json.dumps(result)
    instructions = result["instructions"]
    assert isinstance(instructions, list) and instructions[-1]["action"] == "arrive"


def test_relations_are_preserved_by_xml_parser(tmp_path: Path) -> None:
    path = tmp_path / "restriction.osm"
    path.write_text(
        '<osm><node id="1" lat="0" lon="0"/>'
        '<relation id="2"><member type="node" ref="1" role="via"/>'
        '<tag k="type" v="restriction"/></relation></osm>'
    )
    assert parse_osm(path).relations[0].members == (("node", 1, "via"),)


def test_shortcut_cache_reuse_and_invalidation() -> None:
    graph = build_graph(network([(0, 0), (0.003, 0)], [(1, 2, {})]), 0.001)
    first = pathfind(graph, 1, 2, "car", "shortest")
    keys = set(graph.shortcuts)
    assert keys
    assert pathfind(graph, 1, 2, "car", "shortest").cost == first.cost
    assert set(graph.shortcuts) == keys
    graph.index()
    assert not graph.shortcuts


def test_general_access_yes_does_not_enable_cars_on_footways() -> None:
    graph = build_graph(
        network(
            [(0, 0), (0.001, 0)],
            [
                (1, 2, {"highway": "footway", "access": "yes"}),
            ],
        )
    )
    assert all("car" not in edge.speeds for edge in graph.edges.values())
    assert pathfind(graph, 1, 2, "walking", "shortest").edges


def test_only_straight_restriction_does_not_permit_u_turn() -> None:
    data = network([(0, 0), (0.001, 0), (0.002, 0)], [])
    data = OSMDataset(
        data.checksum,
        data.nodes,
        (OSMWay(10, (1, 2, 3), {"highway": "residential"}),),
        (),
        data.bounds,
        (
            OSMRelation(
                1,
                (("way", 10, "from"), ("node", 2, "via"), ("way", 10, "to")),
                {"type": "restriction", "restriction": "only_straight_on"},
            ),
        ),
    )
    graph = build_graph(data)
    incoming = next(e for e in graph.edges.values() if e.source == 1 and e.target == 2)
    backward = next(e for e in graph.edges.values() if e.source == 2 and e.target == 1)
    forward = next(e for e in graph.edges.values() if e.source == 2 and e.target == 3)
    assert not graph.allowed(incoming.id, backward, "car")
    assert graph.allowed(incoming.id, forward, "car")
