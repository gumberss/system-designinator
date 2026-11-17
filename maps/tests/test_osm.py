import json
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException

from mapinator.osm import parse_osm

SYNTHETIC_OSM = """<?xml version="1.0"?>
<osm version="0.6">
  <node id="1" lat="-23.56" lon="-46.65"><tag k="name" v="Fixture Cafe"/>
    <tag k="amenity" v="cafe"/><tag k="addr:street" v="Fixture Street"/></node>
  <node id="2" lat="-23.561" lon="-46.651"/>
  <node id="3" lat="-23.562" lon="-46.65"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/>
    <tag k="name" v="Fixture Street"/><tag k="oneway" v="yes"/></way>
  <way id="11"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="1"/>
    <tag k="name" v="Fixture Building"/></way>
</osm>"""


def write_fixture(tmp_path: Path, content: str = SYNTHETIC_OSM) -> Path:
    path = tmp_path / "synthetic.osm"
    path.write_text(content, encoding="utf-8")
    return path


def test_nodes_ways_geometry_provenance(tmp_path: Path) -> None:
    dataset = parse_osm(write_fixture(tmp_path))
    assert len(dataset.nodes) == 3
    assert len(dataset.ways) == 2
    assert len(dataset.places) == 3
    assert len(dataset.checksum) == 64
    assert dataset.ways[0].tags["oneway"] == "yes"
    point = dataset.places[0]
    assert json.loads(point.geometry)["coordinates"] == [-46.65, -23.56]
    assert point.address == {"street": "Fixture Street"}
    assert dataset.places[1].location_kind == "representative_point"
    assert json.loads(dataset.places[2].geometry)["type"] == "Polygon"


@pytest.mark.parametrize(
    "content",
    [
        '<osm><node id="1" lat="nan" lon="1"/></osm>',
        '<osm><node id="1" lat="91" lon="1"/></osm>',
        '<osm><node id="1" lat="1" lon="1"/><node id="1" lat="1" lon="2"/></osm>',
        '<osm><node id="1" lat="1" lon="1"/><way id="2"><nd ref="1"/><nd ref="3"/></way></osm>',
        '<osm><node id="1" lat="1" lon="1" action="delete"/></osm>',
        "<osm/>",
        "<not-osm/>",
    ],
)
def test_invalid_inputs_fail_explicitly(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValueError):
        parse_osm(write_fixture(tmp_path, content))


def test_element_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        parse_osm(write_fixture(tmp_path), max_elements=1)


def test_xml_entities_rejected(tmp_path: Path) -> None:
    content = (
        '<!DOCTYPE osm [<!ENTITY x "secret">]><osm><node id="1" lat="1" lon="2">&x;</node></osm>'
    )
    with pytest.raises(DefusedXmlException):
        parse_osm(write_fixture(tmp_path, content))


def test_pbf_is_not_silently_accepted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="XML only"):
        parse_osm(tmp_path / "unsupported.pbf")
