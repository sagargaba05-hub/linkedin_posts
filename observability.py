"""
observability.py — knows when something's wrong and tells Sagar.

Two responsibilities:

1) Error alerting — wraps the top-level main() in a try/except that posts
   a critical error message to Slack with the traceback before re-raising.
   Without this, a crash shows up as a red workflow run that you have to
   actively notice.

2) Token expiry monitoring — LinkedIn tokens last ~60 days. We track when a
   token was first observed (the first time userinfo succeeded), alert at
   55-day mark, and emit a hard alert at 60+ days when the token is presumed
   dead. Plus a reactive 401 handler that always alerts immediately.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import datetime

from slack_sdk import WebClient

from config import (
    LINKEDIN_TOKEN_LIFETIME_DAYS,
    TOKEN_EXPIRY_WARN_DAYS,
    get_logger,
    is_staging,
    now_local,
)

LOG = get_logger("observability")


# --------------------------------------------------------------------------- #
# Slack alerts                                                                #
# --------------------------------------------------------------------------- #


def alert(slack: WebClient, channel_id: str, severity: str, message: str) -> None:
    """Send a critical alert to Slack. Failure to send is swallowed — the
    worst thing we can do during an alert is throw an exception about throwing."""
    icon = {
        "ERROR": ":rotating_light:",
        "WARN": ":warning:",
        "INFO": ":information_source:",
    }.get(severity, ":question:")
    prefix = "[STAGING] " if is_staging() else ""
    text = f"{icon} {prefix}*{severity}* — {message}"
    try:
        slack.chat_postMessage(channel=channel_id, text=text)
        LOG.info("Alert sent: severity=%s msg=%s", severity, message[:100])
    except Exception as e:
        LOG.error("FAILED to send alert (severity=%s): %s", severity, e)


def alert_exception(slack: WebClient, channel_id: str, where: str, exc: BaseException) -> None:
    """Format a traceback nicely and post it. Truncated to fit Slack's 40K limit."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > 3000:
        tb = tb[:3000] + "\n... (truncated)"
    msg = f"Unhandled exception in `{where}`\n```\n{tb}\n```"
    alert(slack, channel_id, "ERROR", msg)


def with_error_alerting(
    slack: WebClient,
    channel_id: str,
    where: str,
) -> Callable:
    """Decorator that catches any exception, alerts to Slack, then re-raises
    so GitHub Actions still shows a red run."""

    def deco(fn: Callable) -> Callable:
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except SystemExit:
                raise
            except BaseException as e:
                alert_exception(slack, channel_id, where, e)
                raise

        return wrapper

    return deco


# --------------------------------------------------------------------------- #
# Token expiry monitoring                                                     #
# --------------------------------------------------------------------------- #


def record_token_first_seen(state_get: Callable, state_set: Callable, member_id: str) -> None:
    """First time we see a successful auth for a given member_id, record the
    timestamp. If the member_id changes (re-auth elsewhere), reset the clock."""
    existing = state_get("linkedin_token_first_seen", {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    if existing.get("member_id") != member_id:
        new_record = {
            "member_id": member_id,
            "first_seen_at": now_local().isoformat(timespec="seconds"),
        }
        state_set("linkedin_token_first_seen", new_record)
        LOG.info("Recorded fresh LinkedIn token first-seen: %s", new_record)


def reset_token_first_seen(state_set: Callable, member_id: str) -> None:
    """Manually reset the token-age clock — call after a 401 + manual rotation
    is acknowledged. Saves the user from getting alerts on the new token."""
    state_set(
        "linkedin_token_first_seen",
        {
            "member_id": member_id,
            "first_seen_at": now_local().isoformat(timespec="seconds"),
        },
    )


def days_since_token_first_seen(state_get: Callable, member_id: str) -> int | None:
    record = state_get("linkedin_token_first_seen", None)
    if not record or not isinstance(record, dict):
        return None
    if record.get("member_id") != member_id:
        return None
    try:
        first_seen = datetime.fromisoformat(record["first_seen_at"])
    except (KeyError, ValueError):
        return None
    delta = now_local() - first_seen
    return delta.days


def maybe_alert_token_expiry(
    slack: WebClient,
    channel_id: str,
    state_get: Callable,
    state_set: Callable,
    member_id: str,
) -> None:
    """If the token is within TOKEN_EXPIRY_WARN_DAYS of the LINKEDIN_TOKEN_LIFETIME_DAYS
    threshold, send a warning to Slack — but only once per token per warning state."""
    days = days_since_token_first_seen(state_get, member_id)
    if days is None:
        return

    days_remaining = LINKEDIN_TOKEN_LIFETIME_DAYS - days
    LOG.info("LinkedIn token age: %d days (≈%d remaining)", days, days_remaining)

    last_alerted = state_get("linkedin_token_last_alert_state", "") or ""

    if days_remaining <= 0:
        new_state = "EXPIRED"
        if last_alerted != new_state:
            alert(
                slack,
                channel_id,
                "ERROR",
                f"LinkedIn token is *>{LINKEDIN_TOKEN_LIFETIME_DAYS} days old* and likely expired. "
                "Daily posts will stop. Regenerate via "
                "<https://www.linkedin.com/developers/apps|LinkedIn dev console> → Auth → "
                "Token Generator (scopes: `w_member_social openid profile`), then update the "
                "`LINKEDIN_TOKEN` secret in GitHub.",
            )
            state_set("linkedin_token_last_alert_state", new_state)
    elif days_remaining <= TOKEN_EXPIRY_WARN_DAYS:
        new_state = f"WARN_{days_remaining}"
        if not last_alerted.startswith("WARN_") or last_alerted != new_state:
            alert(
                slack,
                channel_id,
                "WARN",
                f"LinkedIn token expires in approximately *{days_remaining} day(s)*. "
                "Plan to regenerate before then to avoid daily-post downtime.",
            )
            state_set("linkedin_token_last_alert_state", new_state)
    else:
        # Healthy state — clear any prior alert state
        if last_alerted:
            state_set("linkedin_token_last_alert_state", "")


def alert_token_rejected(slack: WebClient, channel_id: str) -> None:
    """Called when a LinkedIn API call returns 401. Always alerts."""
    alert(
        slack,
        channel_id,
        "ERROR",
        "LinkedIn token was rejected (401). Token has expired or been revoked. "
        "Regenerate via <https://www.linkedin.com/developers/apps|LinkedIn dev console> → "
        "Auth → Token Generator (scopes: `w_member_social openid profile`), then update "
        "the `LINKEDIN_TOKEN` secret in GitHub. Today's draft will retry on the next tick.",
    )
