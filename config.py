"""
config.py — central place for env vars, constants, and logging setup.

Other modules import the configured logger via `from config import get_logger`.
Each module gets its own tagged logger so logs are easy to grep:
    [config], [sheets], [slack], [linkedin], [gen], [pipeline],
    [reliability], [observability], [engagement]
"""

from __future__ import annotations

import logging
import os
import sys
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #


def _configure_root_logging() -> None:
    """Install the root handler exactly once."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_logger(tag: str) -> logging.Logger:
    """Return a tagged logger so every line is prefixed with [tag]."""
    _configure_root_logging()
    return logging.getLogger(tag)


LOG = get_logger("config")


# --------------------------------------------------------------------------- #
# Env vars                                                                    #
# --------------------------------------------------------------------------- #


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        LOG.error("Missing required env var: %s", name)
        sys.exit(1)
    return val or ""


def env_required_all() -> dict[str, str]:
    """Loads + validates all required secrets in one pass. Fails fast on missing."""
    return {
        "GOOGLE_SERVICE_ACCOUNT_JSON": env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True),
        "SHEET_ID": env("SHEET_ID", required=True),
        "ANTHROPIC_API_KEY": env("ANTHROPIC_API_KEY", required=True),
        "SLACK_BOT_TOKEN": env("SLACK_BOT_TOKEN", required=True),
        "SLACK_CHANNEL_ID": env("SLACK_CHANNEL_ID", required=True),
        "LINKEDIN_TOKEN": env("LINKEDIN_TOKEN", required=True),
    }


# --------------------------------------------------------------------------- #
# Models                                                                      #
# --------------------------------------------------------------------------- #

# Writer + planner use Sonnet (creative work). Critic uses Haiku (structured
# evaluation, ~5x cheaper, no measurable quality loss for the verdict step).
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-4-6")
CRITIC_MODEL = env("CRITIC_MODEL", "claude-haiku-4-5-20251001")

# --------------------------------------------------------------------------- #
# Scheduling                                                                  #
# --------------------------------------------------------------------------- #

TZ_NAME = env("TZ", "Australia/Sydney")
DAILY_DRAFT_HOUR = int(env("DAILY_DRAFT_HOUR", "10"))
DRY_RUN = env("DRY_RUN", "0") == "1"

# When set, the script prefixes Slack messages with [STAGING] and uses
# different Slack channel + Sheet IDs so prod isn't affected.
ENVIRONMENT = env("ENVIRONMENT", "production")  # "production" or "staging"

# --------------------------------------------------------------------------- #
# Reliability                                                                 #
# --------------------------------------------------------------------------- #

# Drafts that have been sitting in 'drafted' state with no user reply for this
# many hours are auto-abandoned so they stop polluting future runs.
ABANDONED_DRAFT_HOURS = int(env("ABANDONED_DRAFT_HOURS", "36"))

# LinkedIn token expiry warning threshold (days before expiry to alert)
TOKEN_EXPIRY_WARN_DAYS = int(env("TOKEN_EXPIRY_WARN_DAYS", "5"))
LINKEDIN_TOKEN_LIFETIME_DAYS = 60  # LinkedIn tokens expire after ~60 days

# Circuit breaker config
CIRCUIT_FAIL_MAX = int(env("CIRCUIT_FAIL_MAX", "5"))
CIRCUIT_RESET_TIMEOUT_SEC = int(env("CIRCUIT_RESET_TIMEOUT_SEC", "300"))

# --------------------------------------------------------------------------- #
# Engagement feedback                                                         #
# --------------------------------------------------------------------------- #

# How many top-performing past posts to use as few-shot examples in generation
TOP_POSTS_FEW_SHOT_COUNT = int(env("TOP_POSTS_FEW_SHOT_COUNT", "3"))

# Minimum reach score to qualify as a "top post" worth using as a few-shot
TOP_POST_MIN_REACH = int(env("TOP_POST_MIN_REACH", "5"))

# --------------------------------------------------------------------------- #
# Sheets / state schema                                                       #
# --------------------------------------------------------------------------- #

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

STATE_TAB_NAME = "_state"

LINKEDIN_API_BASE = "https://api.linkedin.com"
LINKEDIN_USERINFO = f"{LINKEDIN_API_BASE}/v2/userinfo"
LINKEDIN_UGC_POSTS = f"{LINKEDIN_API_BASE}/v2/ugcPosts"
LINKEDIN_SOCIAL_ACTIONS = f"{LINKEDIN_API_BASE}/v2/socialActions"

CANONICAL_COLUMNS: dict[str, list[str]] = {
    "sno": ["sno", "sno.", "s.no", "serial", "#"],
    "date": ["date"],
    "topic": ["topic"],
    "angle": ["angle"],
    "key_points": ["key points", "keypoints", "points"],
    "voice": ["voice", "tone"],
    "hook_style": ["hook style", "hook"],
    "link": ["link", "url"],
    "cta": ["cta", "call to action"],
    "status": ["status"],
    "post_url": ["post url", "posturl", "linkedin url", "published"],
    "reach_score": ["reach score", "reachscore", "engagement"],
    "notes": ["notes"],
    "generated_by": ["generated by"],
}

# Project root — used to locate the prompts/ folder
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")


# --------------------------------------------------------------------------- #
# Time helpers                                                                #
# --------------------------------------------------------------------------- #


def now_local():
    from datetime import datetime
    return datetime.now(ZoneInfo(TZ_NAME))


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def is_staging() -> bool:
    return ENVIRONMENT.lower() == "staging"
