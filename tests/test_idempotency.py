"""Tests for the idempotency registry — the layer that prevents duplicate posts."""

from reliability import IdempotencyRegistry, new_idempotency_key, run_once


class FakeStateStore:
    """In-memory stand-in for the _state tab so tests don't need Google Sheets."""

    def __init__(self):
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class TestNewKey:
    def test_returns_uuid_string(self):
        key = new_idempotency_key()
        assert isinstance(key, str)
        assert len(key) == 36  # standard UUID4 length with dashes

    def test_keys_are_unique(self):
        keys = {new_idempotency_key() for _ in range(100)}
        assert len(keys) == 100


class TestIdempotencyRegistry:
    def _registry(self):
        store = FakeStateStore()
        return IdempotencyRegistry(store.get, store.set), store

    def test_has_completed_false_for_new_key(self):
        reg, _ = self._registry()
        assert reg.has_completed("k1", "linkedin_publish") is False

    def test_mark_then_check(self):
        reg, _ = self._registry()
        reg.mark_completed("k1", "linkedin_publish")
        assert reg.has_completed("k1", "linkedin_publish") is True

    def test_different_ops_independent(self):
        reg, _ = self._registry()
        reg.mark_completed("k1", "linkedin_publish")
        assert reg.has_completed("k1", "sheet_status_posted") is False

    def test_different_keys_independent(self):
        reg, _ = self._registry()
        reg.mark_completed("k1", "linkedin_publish")
        assert reg.has_completed("k2", "linkedin_publish") is False

    def test_persists_across_instances(self):
        store = FakeStateStore()
        reg1 = IdempotencyRegistry(store.get, store.set)
        reg1.mark_completed("k1", "linkedin_publish")

        # New instance reading the same store sees the prior write
        reg2 = IdempotencyRegistry(store.get, store.set)
        assert reg2.has_completed("k1", "linkedin_publish") is True


class TestRunOnce:
    def test_runs_first_time(self):
        reg, _ = TestIdempotencyRegistry()._registry()
        called = []

        def expensive():
            called.append(1)
            return "result"

        result = run_once(reg, "k1", "op", expensive)
        assert result == "result"
        assert called == [1]

    def test_skips_second_time(self):
        reg, _ = TestIdempotencyRegistry()._registry()
        called = []

        def expensive():
            called.append(1)
            return "result"

        first = run_once(reg, "k1", "op", expensive)
        second = run_once(reg, "k1", "op", expensive)
        assert first == "result"
        assert second is None  # Skipped due to idempotency
        assert called == [1]   # Only called once
