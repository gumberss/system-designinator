import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from defusedxml.ElementTree import iterparse


@dataclass(frozen=True)
class OSMNode:
    osm_id: int
    latitude: float
    longitude: float
    tags: dict[str, str]


@dataclass(frozen=True)
class OSMWay:
    osm_id: int
    refs: tuple[int, ...]
    tags: dict[str, str]


@dataclass(frozen=True)
class OSMRelation:
    osm_id: int
    members: tuple[tuple[str, int, str], ...]
    tags: dict[str, str]


@dataclass(frozen=True)
class OSMPlace:
    source_id: str
    name: str
    geometry: str
    category: str | None
    address: dict[str, str]
    tags: dict[str, str]
    location_kind: str


@dataclass(frozen=True)
class OSMDataset:
    checksum: str
    nodes: tuple[OSMNode, ...]
    ways: tuple[OSMWay, ...]
    places: tuple[OSMPlace, ...]
    bounds: tuple[float, float, float, float]
    relations: tuple[OSMRelation, ...] = ()


def parse_osm(path: Path, max_elements: int = 200_000) -> OSMDataset:
    if path.suffix.lower() != ".osm":
        raise ValueError("Phase 1 accepts .osm XML only; PBF support is not implemented.")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("OSM input exceeds the phase 1 limit of 100 MiB.")
    nodes: dict[int, OSMNode] = {}
    ways: dict[int, OSMWay] = {}
    relations: dict[int, OSMRelation] = {}
    count = 0
    root = None
    for event, element in iterparse(path, events=("start", "end")):
        if root is None:
            root = element
            if root.tag != "osm":
                raise ValueError("Input must be an OSM XML document.")
        if event != "end" or element.tag not in {"node", "way", "relation"}:
            continue
        count += 1
        if count > max_elements:
            raise ValueError("OSM element limit exceeded; choose a smaller region.")
        if element.get("action") == "delete" or element.get("visible") == "false":
            raise ValueError("OSM change/delete files are not supported.")
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}
        osm_id = int(element.attrib["id"])
        if element.tag == "node":
            lat = float(element.attrib["lat"])
            lon = float(element.attrib["lon"])
            if not math.isfinite(lat) or not math.isfinite(lon):
                raise ValueError("Coordinates must be finite.")
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("Coordinates are outside WGS84 bounds.")
            if osm_id in nodes:
                raise ValueError("Duplicate OSM node ID.")
            nodes[osm_id] = OSMNode(osm_id, lat, lon, tags)
        elif element.tag == "way":
            if osm_id in ways:
                raise ValueError("Duplicate OSM way ID.")
            ways[osm_id] = OSMWay(
                osm_id, tuple(int(nd.attrib["ref"]) for nd in element.findall("nd")), tags
            )
        else:
            if osm_id in relations:
                raise ValueError("Duplicate OSM relation ID.")
            relations[osm_id] = OSMRelation(
                osm_id,
                tuple(
                    (m.attrib["type"], int(m.attrib["ref"]), m.attrib.get("role", ""))
                    for m in element.findall("member")
                ),
                tags,
            )
        element.clear()
        root.clear()
    if not nodes:
        raise ValueError("OSM document contains no nodes.")
    for way in ways.values():
        if len(way.refs) < 2 or any(ref not in nodes for ref in way.refs):
            raise ValueError(f"Way {way.osm_id} has incomplete geometry.")
    places = []
    for node in nodes.values():
        name = node.tags.get("name") or node.tags.get("addr:full")
        if name:
            places.append(
                _place(
                    f"node/{node.osm_id}",
                    name,
                    json.dumps({"type": "Point", "coordinates": [node.longitude, node.latitude]}),
                    node.tags,
                    "osm_node",
                )
            )
    for way in ways.values():
        name = way.tags.get("name") or way.tags.get("addr:full")
        if name:
            places.append(
                _place(
                    f"way/{way.osm_id}",
                    name,
                    way_geometry(way, nodes),
                    way.tags,
                    "representative_point",
                )
            )
    return OSMDataset(
        hashlib.sha256(path.read_bytes()).hexdigest(),
        tuple(nodes.values()),
        tuple(ways.values()),
        tuple(places),
        (
            min(n.longitude for n in nodes.values()),
            min(n.latitude for n in nodes.values()),
            max(n.longitude for n in nodes.values()),
            max(n.latitude for n in nodes.values()),
        ),
        tuple(relations.values()),
    )


def way_geometry(way: OSMWay, nodes: dict[int, OSMNode]) -> str:
    coordinates = [[nodes[ref].longitude, nodes[ref].latitude] for ref in way.refs]
    if len(way.refs) >= 4 and way.refs[0] == way.refs[-1] and "highway" not in way.tags:
        return json.dumps({"type": "Polygon", "coordinates": [coordinates]})
    return json.dumps({"type": "LineString", "coordinates": coordinates})


def _place(
    source_id: str, name: str, geometry: str, tags: dict[str, str], location_kind: str
) -> OSMPlace:
    category = next(
        (tags[key] for key in ("amenity", "shop", "tourism", "leisure", "highway") if key in tags),
        None,
    )
    return OSMPlace(
        source_id,
        name,
        geometry,
        category,
        {key[5:]: value for key, value in tags.items() if key.startswith("addr:")},
        tags,
        location_kind,
    )
