from .base import (
    MAX_EVENTS_PER_SESSION,
    MAX_MEMORIES,
    MAX_MEMORY_CANDIDATES,
    StoreBase,
    normalize_memory_content,
    now,
)
from .materials import MaterialRepositoryMixin
from .memory import MemoryRepositoryMixin
from .runs import RunRepositoryMixin
from .sessions import SessionRepositoryMixin
from nico_application.use_cases import RepositoryPort, UnitOfWorkPort


class Store(
    SessionRepositoryMixin,
    MaterialRepositoryMixin,
    MemoryRepositoryMixin,
    RunRepositoryMixin,
    StoreBase,
    RepositoryPort,
    UnitOfWorkPort,
):
    """Compatibility repository facade over the storage modules."""


__all__ = [
    "MAX_EVENTS_PER_SESSION", "MAX_MEMORIES", "MAX_MEMORY_CANDIDATES",
    "Store", "normalize_memory_content", "now",
]
