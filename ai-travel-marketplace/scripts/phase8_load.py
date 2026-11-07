from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter

import httpx


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


async def _run_load(
    url: str,
    request_count: int,
    concurrency: int,
    timeout_seconds: float,
) -> dict[str, object]:
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    started = time.perf_counter()
    worker_count = min(request_count, concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        limits=httpx.Limits(
            max_connections=worker_count,
            max_keepalive_connections=worker_count,
        ),
    ) as client:

        async def send_requests(count: int) -> None:
            for _ in range(count):
                request_started = time.perf_counter()
                try:
                    response = await client.get(url)
                except httpx.RequestError as error:
                    latencies.append((time.perf_counter() - request_started) * 1000)
                    errors[type(error).__name__] += 1
                else:
                    latencies.append((time.perf_counter() - request_started) * 1000)
                    statuses[str(response.status_code)] += 1

        base_count, remainder = divmod(request_count, worker_count)
        await asyncio.gather(
            *(
                send_requests(base_count + (index < remainder))
                for index in range(worker_count)
            )
        )

    elapsed_seconds = time.perf_counter() - started
    successful = sum(count for status, count in statuses.items() if status.startswith("2"))
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)
    return {
        "endpoint": url,
        "requests": request_count,
        "concurrency": worker_count,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "throughput_requests_per_second": round(request_count / elapsed_seconds, 2),
        "successful_responses": successful,
        "failed_responses": request_count - successful,
        "status_counts": dict(sorted(statuses.items())),
        "transport_errors": dict(sorted(errors.items())),
        "latency_ms": {
            "p50": round(p50, 2) if p50 is not None else None,
            "p95": round(p95, 2) if p95 is not None else None,
            "p99": round(p99, 2) if p99 is not None else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a bounded read-only HTTP load experiment."
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:58080/api/activities/search?city_code=TYO",
        help="Read-only endpoint URL (default: local Activities search via Gateway)",
    )
    parser.add_argument("--requests", type=_positive_integer, default=200)
    parser.add_argument("--concurrency", type=_positive_integer, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    result = asyncio.run(
        _run_load(args.url, args.requests, args.concurrency, args.timeout)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
