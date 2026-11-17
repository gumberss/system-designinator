"""Browser test server with explicitly synthetic data; never a production entry point."""

from datetime import UTC, datetime
from uuid import uuid4

from mapinator.api import Settings, create_app
from mapinator.domain import DatasetInfo, SearchHit
from mapinator.osm import OSMDataset, OSMNode, OSMWay
from mapinator.traffic import TrafficEstimate, TrafficSnapshot
from tests.memory import MemoryRepository
from tests.navigation_memory import MemoryNavigationCache

repo = MemoryRepository()
dataset_id = uuid4()
now = datetime.now(UTC)
repo.hits = [
    SearchHit("node/1", "Fixture Cafe", -23.56, -46.65, "cafe", {}, "osm_node", dataset_id, now)
]
repo.info = DatasetInfo(
    dataset_id,
    "https://example.invalid/synthetic-browser-fixture",
    "synthetic",
    now,
    3,
    1,
    1,
    (-46.651, -23.561, -46.65, -23.56),
    {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Synthetic fixture road"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-46.65, -23.56],
                        [-46.651, -23.561],
                    ],
                },
            },
        ],
    },
)
app = create_app(repo, Settings(allowed_origins="http://127.0.0.1:58181"), MemoryNavigationCache())
repo.road_data = OSMDataset(
    "synthetic-browser-routing",
    (
        OSMNode(1, -23.56, -46.65, {}),
        OSMNode(2, -23.561, -46.651, {}),
        OSMNode(
            3,
            -23.56,
            -46.65,
            {
                "name": "Synthetic Browser Cafe",
                "amenity": "cafe",
                "opening_hours": "24/7",
                "internet_access": "wlan",
            },
        ),
    ),
    (OSMWay(10, (1, 2), {"highway": "residential", "name": "Fixture Street"}),),
    (),
    (-46.651, -23.561, -46.65, -23.56),
)
repo.snapshot = TrafficSnapshot(
    "synthetic-browser-traffic",
    {
        "10:0:0:1": TrafficEstimate("10:0:0:1", 2, now, 1, 5, "heavy"),
    },
)
