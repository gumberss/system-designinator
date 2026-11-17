import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from aiokafka.errors import KafkaError
from defusedxml.common import DefusedXmlException
from sqlalchemy.exc import SQLAlchemyError

from mapinator.api import Coordinate, Settings, _parse_bounds, create_app
from mapinator.benchmark import benchmark, benchmark_http
from mapinator.domain import DomainError
from mapinator.osm import parse_osm
from mapinator.postgres import PostgresRepository, import_dataset, make_engine, migrate
from mapinator.routing import calculate
from mapinator.traffic_worker import initialize_topics, run_worker


def download_osm(bounds: str, output: Path) -> str:
    west, south, east, north = _parse_bounds(bounds)
    if (east - west) * (north - south) > 0.0025:
        raise ValueError("Choose a small extract (bbox area <= 0.0025 square degrees).")
    if output.exists():
        raise ValueError("Output already exists; choose a new file to avoid overwriting.")
    url = f"https://api.openstreetmap.org/api/0.6/map?bbox={west},{south},{east},{north}"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() != ".osm":
        raise ValueError("Download output must have the .osm extension.")
    temporary = output.with_name(output.stem + ".partial.osm")
    if temporary.exists():
        raise ValueError(
            "Partial download exists; inspect/remove that specific file before retrying."
        )
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            for attempt in range(3):
                with client.stream(
                    "GET", url, headers={"User-Agent": "Mapinator-local-lab/0.1"}
                ) as response:
                    if response.status_code in {429, 502, 503, 504} and attempt < 2:
                        time.sleep(2**attempt)
                        continue
                    response.raise_for_status()
                    size = 0
                    with temporary.open("xb") as target:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 100 * 1024 * 1024:
                                raise ValueError("Downloaded XML exceeds 100 MiB.")
                            target.write(chunk)
                    break
        parse_osm(temporary)
        temporary.rename(output)
    except (OSError, ValueError, httpx.HTTPError, DefusedXmlException):
        if temporary.exists():
            temporary.unlink()
        raise
    return url


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="mapinator")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Install/verify the isolated PostGIS schema.")
    serve = commands.add_parser("serve", help="Migrate and start the regional API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=58180)
    download = commands.add_parser(
        "download", help="Explicitly download a small real OSM XML extract."
    )
    download.add_argument("--bbox", default="-46.658,-23.563,-46.652,-23.557")
    download.add_argument("--output", type=Path, default=Path("data") / "sao-paulo.osm")
    ingest = commands.add_parser(
        "import-osm", help="Atomically import a .osm XML extract into PostGIS."
    )
    ingest.add_argument("path", type=Path)
    ingest.add_argument(
        "--source-url", required=True, help="Public provenance URL, without secrets."
    )
    offline = commands.add_parser(
        "route-osm", help="Calculate a real regional route from XML without a database."
    )
    offline.add_argument("path", type=Path)
    offline.add_argument("--source", required=True, help="latitude,longitude")
    offline.add_argument("--destination", required=True, help="latitude,longitude")
    offline.add_argument("--mode", choices=("car", "bicycle", "walking"), default="car")
    offline.add_argument("--strategy", choices=("fastest", "shortest"), default="fastest")
    offline.add_argument("--alternatives", type=int, choices=(0, 1, 2), default=0)
    offline.add_argument("--avoid-tolls", action="store_true")
    offline.add_argument("--avoid-motorways", action="store_true")
    commands.add_parser("init-traffic", help="Create traffic topics with short retention.")
    worker = commands.add_parser("traffic-worker", help="Run durable traffic relay or analytics.")
    worker.add_argument("--role", choices=("relay", "analytics"), required=True)
    load = commands.add_parser(
        "benchmark-osm", help="Measure bounded CPU routing on an actual OSM file."
    )
    load.add_argument("path", type=Path)
    load.add_argument("--requests", type=int, default=20)
    load.add_argument("--concurrency", type=int, default=2)
    load.add_argument("--seed", type=int, default=42)
    load.add_argument("--source", help="Optional fixed latitude,longitude")
    load.add_argument("--destination", help="Optional fixed latitude,longitude")
    http_load = commands.add_parser(
        "benchmark-http", help="Measure bounded saved-route HTTP load on loopback."
    )
    http_load.add_argument("--base-url", default="http://127.0.0.1:58180")
    http_load.add_argument("--requests", type=int, default=20)
    http_load.add_argument("--concurrency", type=int, default=2)
    http_load.add_argument("--source", required=True, help="latitude,longitude")
    http_load.add_argument("--destination", required=True, help="latitude,longitude")
    args = parser.parse_args()
    engine = None
    try:
        if args.command in ("benchmark-osm", "benchmark-http"):

            def point(raw: str | None) -> tuple[float, float] | None:
                if raw is None:
                    return None
                parts = raw.split(",")
                if len(parts) != 2:
                    raise ValueError("Coordinates must be latitude,longitude.")
                parsed = Coordinate(latitude=float(parts[0]), longitude=float(parts[1]))
                return parsed.longitude, parsed.latitude

            if args.command == "benchmark-http":
                http_source, http_destination = point(args.source), point(args.destination)
                if http_source is None or http_destination is None:
                    raise ValueError("Both coordinates required.")
                print(
                    json.dumps(
                        benchmark_http(
                            args.base_url,
                            args.requests,
                            args.concurrency,
                            http_source,
                            http_destination,
                        )
                    )
                )
                return
            result = benchmark(
                args.path,
                args.requests,
                args.concurrency,
                args.seed,
                point(args.source),
                point(args.destination),
            )
            print(json.dumps(result))
            return
        if args.command == "download":
            source = download_osm(args.bbox, args.output)
            print(json.dumps({"file": str(args.output), "source_url": source}))
            return
        if args.command == "route-osm":
            points = []
            for raw in (args.source, args.destination):
                parts = raw.split(",")
                if len(parts) != 2:
                    raise ValueError("Coordinates must be latitude,longitude.")
                coordinate = Coordinate(latitude=float(parts[0]), longitude=float(parts[1]))
                points.append((coordinate.longitude, coordinate.latitude))
            result = calculate(
                parse_osm(args.path),
                points[0],
                points[1],
                args.mode,
                args.strategy,
                args.alternatives,
                args.avoid_tolls,
                args.avoid_motorways,
            )
            print(json.dumps(result))
            return
        config = Settings()
        if args.command == "init-traffic":
            asyncio.run(initialize_topics(config.kafka_bootstrap_servers))
            print(json.dumps({"status": "topics_ready"}))
            return
        url = config.database_url.get_secret_value()
        if not url:
            raise ValueError(
                "Set MAPINATOR_DATABASE_URL; .env is read by Compose, not host commands."
            )
        engine = make_engine(url)
        migrate(engine)
        if args.command == "migrate":
            print(json.dumps({"schema_version": 5, "status": "ready"}))
        elif args.command == "traffic-worker":
            asyncio.run(
                run_worker(args.role, PostgresRepository(engine), config.kafka_bootstrap_servers)
            )
        elif args.command == "import-osm":
            provenance = httpx.URL(args.source_url)
            if provenance.scheme != "https" or provenance.username or provenance.password:
                raise ValueError(
                    "Source must be a public HTTPS provenance URL without credentials."
                )
            dataset = parse_osm(args.path)
            identifier = import_dataset(engine, dataset, args.source_url)
            print(
                json.dumps(
                    {
                        "dataset_id": str(identifier),
                        "checksum": dataset.checksum,
                        "nodes": len(dataset.nodes),
                        "ways": len(dataset.ways),
                        "places": len(dataset.places),
                        "license": "ODbL 1.0",
                    }
                )
            )
        else:
            engine.dispose()
            engine = None
            uvicorn.run(
                create_app(settings=config), host=args.host, port=args.port, access_log=False
            )
    except (
        ValueError,
        OSError,
        httpx.HTTPError,
        SQLAlchemyError,
        DefusedXmlException,
        DomainError,
        KafkaError,
    ) as exc:
        # Driver error strings may contain SQL parameters or credentials.
        detail = str(exc) if isinstance(exc, (ValueError, DomainError)) else type(exc).__name__
        print(json.dumps({"status": "error", "detail": detail}), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        if engine is not None:
            engine.dispose()
