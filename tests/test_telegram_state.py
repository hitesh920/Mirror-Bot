from mirrorbot.telegram import state as state_module
from mirrorbot.telegram.state import ExpiringStore


def test_put_prunes_unrelated_expired_items(monkeypatch):
    now = 100.0
    monkeypatch.setattr(state_module, "monotonic", lambda: now)
    store = ExpiringStore[object](ttl_seconds=10)
    store.put("large-search-snapshot", object())

    now = 111.0
    store.put("new", "value")

    assert "large-search-snapshot" not in store._items
    assert store.get("new") == "value"


def test_store_evicts_oldest_entry_at_capacity(monkeypatch):
    now = 100.0
    monkeypatch.setattr(state_module, "monotonic", lambda: now)
    store = ExpiringStore[str](ttl_seconds=100, max_items=2)
    store.put("first", "one")
    now += 1
    store.put("second", "two")
    now += 1
    store.put("third", "three")

    assert store.get("first") is None
    assert store.get("second") == "two"
    assert store.get("third") == "three"


def test_store_rejects_non_positive_capacity():
    try:
        ExpiringStore[str](ttl_seconds=10, max_items=0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("Expected an invalid capacity to be rejected")
