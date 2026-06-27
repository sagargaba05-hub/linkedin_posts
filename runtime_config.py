"""
runtime_config.py — reads runtime behavior knobs from env vars set by the
workflow YAML, which in turn pulls them from GitHub Actions Repository
Variables (vars.ENABLED, vars.DAILY_DRAFT_HOUR, vars.ENABLED_DAYS).

The control-panel.html page in this repo writes those vars via the GitHub
REST API, so the user can pause / reschedule / change posting days from a
local web page without touching code.

Every knob has a sensible default — if a var is unset, the system behaves
like it always has.
"""

from __future__ import annotations

from config import env, get_logger, now_local

LOG = get_logger("runtime_config")

DEFAULT_ENABLED = "true"
DEFAULT_DAILY_DRAFT_HOUR = "10"
DEFAULT_ENABLED_DAYS = "monday,tuesday,wednesday,thursday,friday"

# Map normalized day strings -> Python weekday() values (Mon=0)
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def is_enabled() -> bool:
    """Master kill-switch. If false, the script does nothing on any tick."""
    raw = env("ENABLED", DEFAULT_ENABLED).strip().lower()
    enabled = raw in ("true", "1", "yes", "on")
    if not enabled:
        LOG.info("Automation is DISABLED via vars.ENABLED=%r", raw)
    return enabled


def get_daily_draft_hour() -> int:
    """0-23 hour in TZ when daily draft fires. Defaults to 10."""
    raw = env("DAILY_DRAFT_HOUR", DEFAULT_DAILY_DRAFT_HOUR).strip()
    try:
        h = int(raw)
        if 0 <= h <= 23:
            return h
    except ValueError:
        pass
    LOG.warning("DAILY_DRAFT_HOUR=%r is invalid; using default %s", raw, DEFAULT_DAILY_DRAFT_HOUR)
    return int(DEFAULT_DAILY_DRAFT_HOUR)


def get_enabled_days() -> set[str]:
    """Set of lowercase day names ('monday'...'sunday') on which the daily
    draft is allowed to fire. Defaults to weekdays."""
    raw = env("ENABLED_DAYS", DEFAULT_ENABLED_DAYS).strip().lower()
    days = set()
    for part in raw.split(","):
        part = part.strip()
        if part in DAY_NAMES:
            days.add(part)
    if not days:
        LOG.warning("ENABLED_DAYS=%r yielded no valid days; falling back to weekdays", raw)
        return set(DEFAULT_ENABLED_DAYS.split(","))
    return days


def is_today_enabled() -> bool:
    """True if today's local weekday is in the ENABLED_DAYS set."""
    today_name = now_local().strftime("%A").lower()
    enabled_days = get_enabled_days()
    allowed = today_name in enabled_days
    if not allowed:
        LOG.info(
            "Today is %s — not in ENABLED_DAYS=%s; daily draft will skip",
            today_name,
            sorted(enabled_days),
        )
    return allowed
