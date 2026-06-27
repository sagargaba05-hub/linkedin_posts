"""Tests for runtime behavior knobs."""

from runtime_config import should_force_daily_draft


class TestForceDailyDraft:
    def test_default_is_disabled(self, monkeypatch):
        monkeypatch.delenv("FORCE_DAILY_DRAFT", raising=False)

        assert should_force_daily_draft() is False

    def test_truthy_values_enable_force_daily_draft(self, monkeypatch):
        monkeypatch.setenv("FORCE_DAILY_DRAFT", "true")

        assert should_force_daily_draft() is True

    def test_false_value_disables_force_daily_draft(self, monkeypatch):
        monkeypatch.setenv("FORCE_DAILY_DRAFT", "false")

        assert should_force_daily_draft() is False
