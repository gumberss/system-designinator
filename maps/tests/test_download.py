from pathlib import Path

import httpx
import pytest

from mapinator.cli import download_osm
from tests.test_osm import SYNTHETIC_OSM


def test_download_writes_validated_xml_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.Client

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        return original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=SYNTHETIC_OSM, request=request)
            )
        )

    monkeypatch.setattr("mapinator.cli.httpx.Client", client_factory)
    output = tmp_path / "extract.osm"
    source = download_osm("-46.658,-23.563,-46.652,-23.557", output)
    assert source.startswith("https://api.openstreetmap.org/")
    assert output.exists()
    assert not list(tmp_path.glob("*.partial.osm"))
    with pytest.raises(ValueError, match="already exists"):
        download_osm("-46.658,-23.563,-46.652,-23.557", output)


def test_invalid_xml_does_not_publish_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = httpx.Client

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        return original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="<osm/>", request=request)
            )
        )

    monkeypatch.setattr("mapinator.cli.httpx.Client", client_factory)
    output = tmp_path / "extract.osm"
    with pytest.raises(ValueError, match="no nodes"):
        download_osm("-46.658,-23.563,-46.652,-23.557", output)
    assert not output.exists()
    assert not list(tmp_path.glob("*.partial.osm"))
