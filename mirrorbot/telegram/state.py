from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class ExpiringItem(Generic[T]):
    value: T
    expires_at: float


class ExpiringStore(Generic[T]):
    def __init__(self, ttl_seconds: int, *, max_items: int = 128):
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: dict[str, ExpiringItem[T]] = {}

    def put(self, key: str, value: T) -> None:
        now = monotonic()
        self._prune(now)
        if key not in self._items and len(self._items) >= self.max_items:
            oldest = min(
                self._items,
                key=lambda stored: self._items[stored].expires_at,
            )
            self._items.pop(oldest, None)
        self._items[key] = ExpiringItem(value, now + self.ttl_seconds)

    def take(self, key: str) -> T | None:
        self._prune(monotonic())
        item = self._items.pop(key, None)
        if item is None:
            return None
        return item.value

    def get(self, key: str) -> T | None:
        self._prune(monotonic())
        item = self._items.get(key)
        if item is None:
            return None
        return item.value

    def _prune(self, now: float) -> list[tuple[str, T]]:
        expired = [
            (key, item.value)
            for key, item in self._items.items()
            if item.expires_at <= now
        ]
        for key, _ in expired:
            self._items.pop(key, None)
        return expired

    def pop_expired(self) -> list[tuple[str, T]]:
        return self._prune(monotonic())
