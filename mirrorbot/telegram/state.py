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
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, ExpiringItem[T]] = {}

    def put(self, key: str, value: T) -> None:
        self._items[key] = ExpiringItem(value, monotonic() + self.ttl_seconds)

    def take(self, key: str) -> T | None:
        item = self._items.pop(key, None)
        if item is None or item.expires_at <= monotonic():
            return None
        return item.value

    def get(self, key: str) -> T | None:
        item = self._items.get(key)
        if item is None:
            return None
        if item.expires_at <= monotonic():
            self._items.pop(key, None)
            return None
        return item.value

    def pop_expired(self) -> list[tuple[str, T]]:
        now = monotonic()
        expired = [(key, item.value) for key, item in self._items.items() if item.expires_at <= now]
        for key, _ in expired:
            self._items.pop(key, None)
        return expired
