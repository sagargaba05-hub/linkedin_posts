"""Tests for the pending-row picker in pipeline.py."""

from pipeline import _effective_sno, _select_next_pending


class TestEffectiveSno:
    def test_uses_sno_when_present(self):
        row = {"sno": "42", "row_number": 5}
        assert _effective_sno(row) == "42"

    def test_falls_back_to_row_number_when_sno_blank(self):
        row = {"sno": "", "row_number": 7}
        assert _effective_sno(row) == "row-7"

    def test_falls_back_to_row_number_when_sno_missing(self):
        row = {"row_number": 9}
        assert _effective_sno(row) == "row-9"


class TestSelectNextPending:
    def _row(self, **overrides):
        base = {
            "sno": "1",
            "row_number": 2,
            "topic": "AI governance",
            "date": "2026-05-10",
        }
        base.update(overrides)
        return base

    def test_returns_none_when_no_rows(self):
        assert _select_next_pending([], set()) is None

    def test_skips_empty_topic(self):
        rows = [self._row(topic="", row_number=2)]
        assert _select_next_pending(rows, set()) is None

    def test_skips_in_flight_sno(self):
        rows = [self._row(sno="1", row_number=2)]
        assert _select_next_pending(rows, {"1"}) is None

    def test_picks_blank_sno_via_row_number(self):
        rows = [self._row(sno="", row_number=3)]
        pick = _select_next_pending(rows, set())
        assert pick is not None
        assert pick["sno"] == "row-3"

    def test_skips_blank_sno_when_row_already_in_flight(self):
        """Bug from earlier: blank-SNo rows being re-picked. Now they get
        a stable row-N ID, and that ID can sit in the in-flight set."""
        rows = [self._row(sno="", row_number=3)]
        assert _select_next_pending(rows, {"row-3"}) is None

    def test_prefers_earlier_date(self):
        rows = [
            self._row(sno="A", date="2026-05-15", row_number=2),
            self._row(sno="B", date="2026-05-10", row_number=3),
            self._row(sno="C", date="2026-05-20", row_number=4),
        ]
        pick = _select_next_pending(rows, set())
        assert pick["sno"] == "B"

    def test_handles_missing_date_via_row_number(self):
        rows = [
            self._row(sno="A", date="", row_number=5),
            self._row(sno="B", date="", row_number=2),
        ]
        pick = _select_next_pending(rows, set())
        # Both have no date, lower row number wins
        assert pick["sno"] == "B"
