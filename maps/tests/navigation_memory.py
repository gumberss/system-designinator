from datetime import UTC, datetime, timedelta
from uuid import UUID

from mapinator.domain import DomainError
from mapinator.navigation import NavigationState


class MemoryNavigationCache:
    """Test-only latest-state cache, including expiry and CAS."""

    def __init__(self) -> None:
        self.records: dict[tuple[UUID, UUID], tuple[NavigationState, datetime]] = {}
        self.available = True

    async def health(self) -> None:
        if not self.available:
            raise DomainError(503, "navigation_cache_unavailable", "Simulated outage.")

    async def get(self, owner: UUID, session: UUID) -> NavigationState | None:
        await self.health()
        record = self.records.get((owner, session))
        return record[0].model_copy(deep=True) if record and record[1] > datetime.now(UTC) else None

    async def put(self, owner: UUID, session: UUID, state: NavigationState, expected: int) -> bool:
        existing = await self.get(owner, session)
        if (existing.revision if existing else -1) != expected:
            return False
        self.records[owner, session] = (
            state.model_copy(deep=True),
            datetime.now(UTC) + timedelta(minutes=5),
        )
        return True

    async def delete(self, owner: UUID, session: UUID) -> None:
        await self.health()
        self.records.pop((owner, session), None)

    async def close(self) -> None:
        pass
