import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from mapinator.routing import Graph

TrafficSource = Literal["baseline", "historical", "realtime", "mixed"]


class TrafficEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    event_id: UUID
    session_id: UUID
    contributor: str = Field(pattern=r"^[a-f0-9]{64}$")
    graph_version: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=0, le=2**63 - 1)
    observed_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_mps: float = Field(ge=0, le=70)
    accuracy_m: float = Field(ge=0, le=100_000)
    heading_deg: float = Field(ge=0, lt=360)

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Observed time must include timezone.")
        return value


@dataclass(frozen=True)
class TrafficSample:
    contributor: str
    speed_mps: float
    observed_at: datetime


@dataclass(frozen=True)
class TrafficEstimate:
    edge_id: str
    speed_mps: float | None = None
    observed_at: datetime | None = None
    confidence: float = 0
    contributors: int = 0
    congestion: str = "unknown"
    anomaly: bool = False
    historical_speed_mps: float | None = None
    historical_confidence: float = 0


@dataclass(frozen=True)
class TrafficSnapshot:
    version: str = "baseline"
    estimates: dict[str, TrafficEstimate] = field(default_factory=dict)
    unavailable: bool = False


def match_observation(graph: "Graph", event: TrafficEvent, now: datetime) -> str | None:
    from mapinator.routing import distance

    if not -15 <= (now - event.observed_at).total_seconds() <= 900:
        return None
    if event.accuracy_m > 35 or event.speed_mps < 1:
        return None
    point = (event.longitude, event.latitude)
    matches = []
    for edge in graph.edges.values():
        baseline = edge.speeds.get("car")
        if baseline is None or event.speed_mps > baseline * 1.5:
            continue
        a, b = graph.nodes[edge.source], graph.nodes[edge.target]
        scale = math.cos(math.radians(point[1]))
        dx, dy = (b[0] - a[0]) * scale, b[1] - a[1]
        if not dx * dx + dy * dy:
            continue
        heading = math.degrees(math.atan2(dx, dy)) % 360
        delta = abs((heading - event.heading_deg + 180) % 360 - 180)
        if delta > 45:
            continue
        fraction = max(
            0.0,
            min(
                1.0, ((point[0] - a[0]) * scale * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy)
            ),
        )
        projected = (a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1]))
        error = distance(point, projected)
        if error <= max(10, event.accuracy_m):
            matches.append((error, edge.id))
    matches.sort()
    if not matches or (len(matches) > 1 and matches[1][0] - matches[0][0] <= 3):
        return None
    return matches[0][1]


def aggregate(
    edge_id: str,
    samples: list[TrafficSample],
    baseline: float,
    now: datetime,
    previous: TrafficEstimate | None = None,
) -> TrafficEstimate | None:
    latest: dict[str, TrafficSample] = {}
    for sample in samples:
        if not 0 <= (now - sample.observed_at).total_seconds() <= 300:
            continue
        if not math.isfinite(sample.speed_mps) or not 1 <= sample.speed_mps <= baseline * 1.5:
            continue
        if (
            sample.contributor not in latest
            or sample.observed_at > latest[sample.contributor].observed_at
        ):
            latest[sample.contributor] = sample
    if len(latest) < 5:
        return None
    median = statistics.median(s.speed_mps for s in latest.values())
    deviation = statistics.median(abs(s.speed_mps - median) for s in latest.values())
    speeds = [
        s.speed_mps for s in latest.values() if abs(s.speed_mps - median) <= max(2, 3 * deviation)
    ]
    if len(speeds) < 5:
        return None
    speed = statistics.fmean(speeds)
    observed_at = max(s.observed_at for s in latest.values())
    if previous and previous.observed_at and (now - previous.observed_at).total_seconds() <= 300:
        speed = (
            0.25 * speed + 0.75 * (previous.speed_mps or speed)
            if observed_at > previous.observed_at
            else previous.speed_mps or speed
        )
    ratio = speed / baseline
    congestion = "heavy" if ratio < 0.35 else "moderate" if ratio < 0.7 else "free"
    # Hysteresis near class boundaries prevents one small fluctuation changing state.
    if previous and previous.congestion in {"heavy", "moderate", "free"}:
        boundary = 0.35 if "heavy" in {previous.congestion, congestion} else 0.7
        if abs(ratio - boundary) < 0.05:
            congestion = previous.congestion
    return TrafficEstimate(
        edge_id,
        speed,
        observed_at,
        min(1.0, len(speeds) / 10),
        len(speeds),
        congestion,
        ratio < 0.25,
    )


def effective_speed(
    baseline: float, estimate: TrafficEstimate, now: datetime
) -> tuple[float, TrafficSource]:
    travel = 1 / baseline
    source: TrafficSource = "baseline"
    if estimate.historical_speed_mps and estimate.historical_confidence > 0:
        weight = max(0.0, min(1.0, estimate.historical_confidence))
        travel = (1 - weight) * travel + weight / estimate.historical_speed_mps
        source = "historical"
    if estimate.speed_mps and estimate.observed_at:
        age = (now - estimate.observed_at).total_seconds()
        if 0 <= age <= 300:
            weight = min(1.0, max(0.0, estimate.confidence)) * math.exp(-age / 120)
            if weight > 0:
                travel = (1 - weight) * travel + weight / estimate.speed_mps
                source = "realtime"
    return 1 / travel, source


def apply_snapshot(
    graph: "Graph", snapshot: TrafficSnapshot, now: datetime
) -> dict[str, TrafficSource]:
    sources: dict[str, TrafficSource] = {}
    for identifier, estimate in snapshot.estimates.items():
        edge = graph.edges.get(identifier)
        if edge is None or "car" not in edge.speeds:
            continue
        speed, source = effective_speed(edge.speeds["car"], estimate, now)
        graph.edges[identifier] = replace(edge, speeds={**edge.speeds, "car": speed})
        sources[identifier] = source
    graph.index()
    return sources


def utc_bucket(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    return value.replace(minute=value.minute // 5 * 5, second=0, microsecond=0)
