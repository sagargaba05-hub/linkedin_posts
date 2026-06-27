"""Tests for shared retry classification."""

import reliability


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeGSpreadAPIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"APIError: [{status_code}]")
        self.response = FakeResponse(status_code)


class TestRetryClassification:
    def setup_method(self):
        self._original_gspread_error = reliability.GSpreadAPIError
        reliability.GSpreadAPIError = FakeGSpreadAPIError

    def teardown_method(self):
        reliability.GSpreadAPIError = self._original_gspread_error

    def test_retries_google_sheets_service_unavailable(self):
        assert reliability._should_retry(FakeGSpreadAPIError(503)) is True

    def test_retries_google_sheets_rate_limit(self):
        assert reliability._should_retry(FakeGSpreadAPIError(429)) is True

    def test_does_not_retry_google_sheets_auth_error(self):
        assert reliability._should_retry(FakeGSpreadAPIError(403)) is False
