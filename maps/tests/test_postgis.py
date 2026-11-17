import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from mapinator.domain import DomainError, Observation
from mapinator.osm import parse_osm
from mapinator.postgres import PostgresRepository, import_dataset, make_engine, migrate
from mapinator.routing import calculate
from tests.test_osm import write_fixture

pytestmark = pytest.mark.integration


def test_real_postgis_transactions_and_geocoding(tmp_path: Path) -> None:
    url = os.environ.get("MAPINATOR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MAPINATOR_TEST_DATABASE_URL to an isolated disposable PostGIS database.")
    engine = make_engine(url)
    repository = PostgresRepository(engine)
    try:
        migrate(engine)
        migrate(engine)
        repository.health()
        now = datetime.now(UTC)
        identity = repository.create_identity(str(uuid4()), str(uuid4()), now)
        tracker = repository.start_tracking(identity.owner_id, now)
        value = Observation(uuid4(), tracker.session_id, 1, now, -23.56, -46.65, 10, None, None)
        assert repository.save_location(identity.owner_id, value, now)
        assert not repository.save_location(identity.owner_id, value, now)
        with pytest.raises(DomainError):
            repository.save_location(uuid4(), value, now)
        assert repository.current_location(identity.owner_id, now) == value
        assert repository.current_location(identity.owner_id, now + timedelta(minutes=6)) is None
        repository.stop_tracking(identity.owner_id)
        assert repository.current_location(identity.owner_id, now) is None
        dataset = parse_osm(write_fixture(tmp_path))
        identifier = import_dataset(
            engine, dataset, "https://example.invalid/synthetic-unit-fixture"
        )
        assert (
            import_dataset(engine, dataset, "https://example.invalid/synthetic-unit-fixture")
            == identifier
        )
        hits = repository.search("Fixture", None, 10, 0)
        assert len(hits) == 3
        assert repository.search("Fixture", (0, 0, 1, 1), 10, 0) == []
        info = repository.dataset()
        assert info is not None and info.node_count == 3
        dataset_id, roads = repository.routing_dataset()
        assert dataset_id == identifier and roads.checksum == dataset.checksum
        result = calculate(roads, (-46.65, -23.56), (-46.651, -23.561), "car", "shortest")
        route_id = repository.save_route(identity.owner_id, identifier, result, now)
        saved = repository.route(identity.owner_id, route_id, now)
        assert saved is not None and saved["route_id"] == str(route_id)
        assert repository.route(uuid4(), route_id, now) is None
        assert repository.route(identity.owner_id, route_id, now + timedelta(days=2)) is None
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM osm_datasets WHERE active")).scalar()
                == 1
            )
        repository.cleanup(now + timedelta(days=2))
    finally:
        repository.close()
