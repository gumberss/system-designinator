from pathlib import Path

import pytest

from mapinator.benchmark import benchmark, benchmark_http, percentile


def test_percentiles_include_denominator_and_empty() -> None:
    assert percentile([], 0.95) is None
    assert percentile([3, 1, 2], 0.5) == 2
    assert percentile([3, 1, 2], 0.95) == 3
    with pytest.raises(ValueError, match="loopback"):
        benchmark_http("https://external.invalid", 1, 1, (0, 0), (0.004, 0))
    with pytest.raises(ValueError):
        benchmark_http("http://127.0.0.1:58180", 1, 2, (0, 0), (0.004, 0))


def test_workload_bounds_and_real_process_measurement(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-benchmark.osm"
    path.write_text(
        '<osm><node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="0.004"/>'
        '<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>'
    )
    with pytest.raises(ValueError):
        benchmark(path, 1, 2)
    result = benchmark(path, 2, 1, source=(0, 0), destination=(0.004, 0))
    assert result["completed"] == 2 and result["successes"] == 2
    rss, cpu = result["max_sampled_worker_rss_bytes"], result["worker_cpu_s"]
    assert isinstance(rss, int) and rss > 0
    assert isinstance(cpu, (int, float)) and cpu >= 0
