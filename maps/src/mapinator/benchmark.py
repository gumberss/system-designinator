import math
import platform
import random
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil

from mapinator.domain import DomainError
from mapinator.osm import OSMDataset, parse_osm
from mapinator.routing import build_graph, calculate


@dataclass(frozen=True)
class Measurement:
    latency_s: float
    cpu_s: float
    sampled_rss_bytes: int
    error: str | None
    expanded_states: int


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def measure(
    dataset: OSMDataset,
    source: tuple[float, float],
    destination: tuple[float, float],
    submitted: float,
) -> Measurement:
    process = psutil.Process()
    samples = [process.memory_info().rss]
    stopped = threading.Event()

    def sample() -> None:
        while not stopped.wait(0.01):
            samples.append(process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    cpu = time.process_time()
    error = None
    expanded = 0
    try:
        result = calculate(dataset, source, destination, "car", "fastest", 0, False, False)
        count = result["expanded_states"]
        if not isinstance(count, int):
            raise ValueError("Routing returned an invalid expanded-state count.")
        expanded = count
    except DomainError as exc:
        error = exc.code
    finally:
        stopped.set()
        sampler.join()
    return Measurement(
        time.perf_counter() - submitted,
        time.process_time() - cpu,
        max(samples + [process.memory_info().rss]),
        error,
        expanded,
    )


def benchmark(
    path: Path,
    requests: int,
    concurrency: int,
    seed: int = 42,
    source: tuple[float, float] | None = None,
    destination: tuple[float, float] | None = None,
) -> dict[str, object]:
    if not 1 <= requests <= 500 or not 1 <= concurrency <= 4 or concurrency > requests:
        raise ValueError("Requests 1..500, concurrency 1..4 and no greater than requests required.")
    if (source is None) != (destination is None):
        raise ValueError("Supply both source and destination, or neither.")
    dataset = parse_osm(path)
    graph = build_graph(dataset)
    candidates = [e for e in graph.edges.values() if "car" in e.speeds]
    if not candidates:
        raise ValueError("Dataset has no car edges.")
    rng = random.Random(seed)
    pairs = []
    for _ in range(requests):
        if source is not None and destination is not None:
            pairs.append((source, destination))
        else:
            start, end = rng.choice(candidates), rng.choice(candidates)
            pairs.append((graph.nodes[start.source], graph.nodes[end.target]))
    results: list[Measurement] = []
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        pending = set()
        iterator = iter(pairs)
        for _ in range(concurrency):
            a, b = next(iterator)
            pending.add(pool.submit(measure, dataset, a, b, time.perf_counter()))
        while pending:
            ready, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                results.append(future.result())
                pair = next(iterator, None)
                if pair is not None:
                    a, b = pair
                    pending.add(pool.submit(measure, dataset, a, b, time.perf_counter()))
    duration = time.perf_counter() - started
    latencies = [r.latency_s for r in results]
    successful = [r.latency_s for r in results if r.error is None]
    errors = dict(Counter(r.error for r in results if r.error is not None))
    return {
        "scope": "offline CPU routing only; not HTTP/DB/Kafka/Redis/LLM load",
        "dataset": {
            "sha256": dataset.checksum,
            "file_bytes": path.stat().st_size,
            "nodes": len(dataset.nodes),
            "directed_edges": len(graph.edges),
            "segments": len(graph.segments),
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "logical_cpus": psutil.cpu_count(),
        },
        "workload": {
            "requests": requests,
            "concurrency": concurrency,
            "seed": seed,
            "fixed_pair": source is not None,
            "graph_rebuilt_per_request": True,
        },
        "completed": len(results),
        "successes": len(successful),
        "errors_by_code": errors,
        "duration_s": duration,
        "completed_rps": len(results) / duration,
        "successful_rps": len(successful) / duration,
        "latency_s": {
            "p50_all": percentile(latencies, 0.5),
            "p95_all": percentile(latencies, 0.95),
            "p99_all": percentile(latencies, 0.99),
            "p95_success": percentile(successful, 0.95),
        },
        "worker_cpu_s": sum(r.cpu_s for r in results),
        "max_sampled_worker_rss_bytes": max(r.sampled_rss_bytes for r in results),
        "max_expanded_states": max(r.expanded_states for r in results),
        "limitations": [
            "RSS sampled at 10ms, not an absolute peak.",
            "Bounded admission: at most concurrency jobs in flight.",
            "Cold worker startup/pickling included in latency; graph cache not reused.",
            "No throughput/global-capacity or availability guarantee inferred.",
            "All latencies include unsuccessful attempts; successful percentile is separate.",
        ],
    }


def http_attempt(
    base_url: str,
    source: tuple[float, float],
    destination: tuple[float, float],
    submitted: float,
) -> tuple[float, int, str | None]:
    try:
        with httpx.Client(timeout=12, follow_redirects=False) as client:
            headers = {"origin": base_url}
            identity = client.post(base_url + "/api/v1/auth/session", headers=headers)
            if identity.status_code not in (200, 201):
                return time.perf_counter() - submitted, identity.status_code, "auth_failed"
            csrf = identity.json().get("csrf_token")
            if not isinstance(csrf, str):
                raise ValueError("Authentication response has no CSRF token.")
            response = client.post(
                base_url + "/api/v1/routes",
                headers={
                    **headers,
                    "x-csrf-token": csrf,
                },
                json={
                    "source": {"longitude": source[0], "latitude": source[1]},
                    "destination": {"longitude": destination[0], "latitude": destination[1]},
                },
            )
            code = None
            if response.status_code == 201:
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("geometry"), dict):
                    raise ValueError("Route response has no geometry.")
                if data["geometry"].get("type") != "LineString" or not isinstance(
                    data.get("graph_version"), str
                ):
                    raise ValueError("Route response violates the benchmark contract.")
            else:
                error = response.json().get("code")
                if not isinstance(error, str):
                    raise ValueError("Error response has no error code.")
                code = error
            return time.perf_counter() - submitted, response.status_code, code
    except httpx.HTTPError as exc:
        return time.perf_counter() - submitted, 0, type(exc).__name__


def benchmark_http(
    base_url: str,
    requests: int,
    concurrency: int,
    source: tuple[float, float],
    destination: tuple[float, float],
) -> dict[str, object]:
    url = httpx.URL(base_url)
    if (
        url.scheme not in ("http", "https")
        or url.host not in ("127.0.0.1", "localhost", "::1")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("HTTP benchmark requires a credential-free loopback base URL.")
    if not 1 <= requests <= 500 or not 1 <= concurrency <= 4 or concurrency > requests:
        raise ValueError("Requests 1..500, concurrency 1..4 and <=requests required.")
    base_url = base_url.rstrip("/")
    started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        pending = {
            pool.submit(http_attempt, base_url, source, destination, time.perf_counter())
            for _ in range(concurrency)
        }
        submitted = concurrency
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                results.append(future.result())
                if submitted < requests:
                    pending.add(
                        pool.submit(
                            http_attempt, base_url, source, destination, time.perf_counter()
                        )
                    )
                    submitted += 1
    elapsed = time.perf_counter() - started
    success = [latency for latency, status, _ in results if status == 201]
    return {
        "scope": "HTTP authentication + saved routes; server resources not measured",
        "base_url": base_url,
        "requests": requests,
        "concurrency": concurrency,
        "completed": len(results),
        "successes": len(success),
        "status_counts": dict(Counter(str(status) for _, status, _ in results)),
        "errors_by_code": dict(Counter(code for _, _, code in results if code is not None)),
        "duration_s": elapsed,
        "successful_rps": len(success) / elapsed,
        "p95_all_s": percentile([latency for latency, _, _ in results], 0.95),
        "p95_success_s": percentile(success, 0.95),
        "limitations": [
            "Creates owned expiring browser sessions/routes; use a dedicated benchmark instance.",
            "No coordinates/tokens/route IDs emitted; only loopback targets allowed.",
            "Check dataset/provenance separately; test fixtures do not prove real capacity.",
            "Client latency includes auth/TCP/routing/persistence, not WS/GPS/EV/LLM workloads.",
        ],
    }
