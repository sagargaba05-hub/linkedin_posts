"""
reliability.py — three layers of protection against transient failures and
duplicated work:

1) Retries (tenacity) — wraps individual API calls so a single 500 doesn't fail
   the whole tick. Exponential backoff between attempts.

2) Circuit breakers (pybreaker) — if a service is genuinely down (5+ failures
   in a row), stop hammering it. Opens for 5 min, then half-opens to test.

3) Idempotency keys — every draft gets a UUID at creation. Before any external
   mutation (LinkedIn POST, sheet write, Slack post-followup), we check whether
   that op was already done for this key. Prevents duplicate posts even if
   GitHub Actions misfires or two ticks overlap.

The idempotency registry lives in the _state tab of the Google Sheet so it
survives runs. Keys older than 30 days are GC'd.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

import pybreaker
import requests
from anthropic import APIError as AnthropicAPIError
from anthropic import APITimeoutError as AnthropicTimeoutError
from slack_sdk.errors import SlackApiError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

try:
    from gspread.exceptions import APIError as GSpreadAPIError
except ImportError:  # pragma: no cover - keeps SDK-mocked unit tests importable
    class GSpreadAPIError(Exception):
        pass

from config import (
    CIRCUIT_FAIL_MAX,
    CIRCUIT_RESET_TIMEOUT_SEC,
    get_logger,
    now_local,
)

LOG = get_logger("reliability")

T = TypeVar("T")

# --------------------------------------------------------------------------- #
# Retry decorators                                                            #
# --------------------------------------------------------------------------- #

# Errors we consider "transient" — worth retrying. Auth errors (401/403) are
# NOT transient and should fail fast so the user is alerted.
TRANSIENT_REQUESTS_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _is_instance_of(exc: Exception, candidates: Any) -> bool:
    if not isinstance(candidates, tuple):
        candidates = (candidates,)
    return any(isinstance(candidate, type) and isinstance(exc, candidate) for candidate in candidates)


def _status_code_from_response(response: Any) -> int | None:
    status_code = getattr(response, "status_code", None)
    if status_code is None and isinstance(response, dict):
        status_code = response.get("status_code") or response.get("code")
    if status_code is None:
        return None
    try:
        return int(status_code)
    except (TypeError, ValueError):
        return None


def _is_transient_api_error(exc: Exception) -> bool:
    """Only retry rate limits and temporary server-side API failures."""
    if _is_instance_of(exc, requests.exceptions.HTTPError):
        status_code = _status_code_from_response(getattr(exc, "response", None))
        if status_code is None:
            return True
        return status_code >= 500 or status_code == 429
    if _is_instance_of(exc, GSpreadAPIError):
        status_code = getattr(exc, "code", None)
        if status_code is None:
            status_code = _status_code_from_response(getattr(exc, "response", None))
        if status_code is None:
            return True
        return status_code >= 500 or status_code == 429
    if _is_instance_of(exc, SlackApiError):
        # Slack returns errors in resp.data; treat rate-limited and ephemeral as retryable
        err = exc.response.get("error", "") if exc.response else ""
        return err in ("ratelimited", "service_unavailable", "fatal_error")
    return True


def _should_retry(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return False
    if _is_instance_of(exc, TRANSIENT_REQUESTS_EXCEPTIONS):
        return True
    if _is_instance_of(
        exc,
        (
            requests.exceptions.HTTPError,
            GSpreadAPIError,
            SlackApiError,
            AnthropicAPIError,
            AnthropicTimeoutError,
        ),
    ):
        return _is_transient_api_error(exc)
    return False


def with_http_retries(fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap a function that calls an HTTP API. Retries 3 times with backoff
    on transient errors only. Fails fast on auth/4xx errors."""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_should_retry),
        before_sleep=before_sleep_log(LOG, "WARNING"),
        reraise=True,
    )
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Circuit breakers — one per external service                                 #
# --------------------------------------------------------------------------- #

linkedin_breaker = pybreaker.CircuitBreaker(
    fail_max=CIRCUIT_FAIL_MAX,
    reset_timeout=CIRCUIT_RESET_TIMEOUT_SEC,
    name="linkedin",
)
slack_breaker = pybreaker.CircuitBreaker(
    fail_max=CIRCUIT_FAIL_MAX,
    reset_timeout=CIRCUIT_RESET_TIMEOUT_SEC,
    name="slack",
)
anthropic_breaker = pybreaker.CircuitBreaker(
    fail_max=CIRCUIT_FAIL_MAX,
    reset_timeout=CIRCUIT_RESET_TIMEOUT_SEC,
    name="anthropic",
)
sheets_breaker = pybreaker.CircuitBreaker(
    fail_max=CIRCUIT_FAIL_MAX,
    reset_timeout=CIRCUIT_RESET_TIMEOUT_SEC,
    name="sheets",
)


def with_circuit(breaker: pybreaker.CircuitBreaker) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator factory binding a function to a specific service's breaker."""

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return breaker.call(fn, *args, **kwargs)

        return wrapper

    return deco


# --------------------------------------------------------------------------- #
# Idempotency registry                                                        #
# --------------------------------------------------------------------------- #


def new_idempotency_key() -> str:
    """Generate a fresh UUID for a new draft."""
    return str(uuid.uuid4())


class IdempotencyRegistry:
    """Records which mutating operations have completed for which idempotency keys.

    State shape (in the _state tab under key='idempotency'):
        {
            "<idempotency_key>": {
                "<op_name>": "<iso_timestamp>",
                ...
            },
            ...
        }

    op_name examples: "linkedin_publish", "sheet_status_posted", "sheet_status_rejected"
    """

    def __init__(self, state_get: Callable, state_set: Callable):
        self._get = state_get
        self._set = state_set
        self._cache: dict[str, dict[str, str]] | None = None

    def _load(self) -> dict[str, dict[str, str]]:
        if self._cache is None:
            raw = self._get("idempotency", {}) or {}
            self._cache = raw if isinstance(raw, dict) else {}
        return self._cache

    def has_completed(self, key: str, op: str) -> bool:
        """True if this op has already been done for this key."""
        registry = self._load()
        completed = registry.get(key, {}).get(op)
        if completed:
            LOG.info("Idempotency hit: key=%s op=%s already_done=%s", key, op, completed)
            return True
        return False

    def mark_completed(self, key: str, op: str) -> None:
        registry = self._load()
        registry.setdefault(key, {})[op] = now_local().isoformat(timespec="seconds")
        self._set("idempotency", registry)
        LOG.info("Idempotency marked: key=%s op=%s", key, op)

    def gc_old_keys(self, days: int = 30) -> int:
        """Remove idempotency keys whose newest op is older than `days`."""
        registry = self._load()
        cutoff = now_local() - timedelta(days=days)
        removed = 0
        for key in list(registry.keys()):
            ops = registry[key]
            try:
                latest = max(datetime.fromisoformat(ts) for ts in ops.values())
            except (ValueError, KeyError):
                continue
            if latest < cutoff:
                del registry[key]
                removed += 1
        if removed:
            self._set("idempotency", registry)
            LOG.info("Idempotency GC removed %d old keys", removed)
        return removed


def guard(registry: IdempotencyRegistry, key: str, op: str) -> Callable:
    """Decorator that runs the wrapped function only if (key, op) hasn't been
    completed yet. Marks completion on success.

    Usage:
        @guard(registry, draft["idempotency_key"], "linkedin_publish")
        def do_publish(): ...

    For functions that need to return a value, use the explicit form below
    (run_once)."""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if registry.has_completed(key, op):
                return None
            result = fn(*args, **kwargs)
            registry.mark_completed(key, op)
            return result

        return wrapper

    return deco


def run_once(
    registry: IdempotencyRegistry,
    key: str,
    op: str,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T | None:
    """Run fn(*args, **kwargs) only if (key, op) hasn't been completed.
    Returns the result, or None if skipped due to idempotency."""
    if registry.has_completed(key, op):
        return None
    result = fn(*args, **kwargs)
    registry.mark_completed(key, op)
    return result
