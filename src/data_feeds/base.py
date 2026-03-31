"""Abstract base class for all data feeds."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DataFeed(ABC):
    """
    All data feeds implement this interface so the probability engine
    can swap feeds or mock them in tests without changing calling code.
    """

    @abstractmethod
    async def fetch(self, *args, **kwargs):
        """Fetch data from the external source."""
        ...

    @abstractmethod
    def is_healthy(self) -> bool:
        """Return True if the feed is currently reachable and returning valid data."""
        ...
