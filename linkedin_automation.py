"""
linkedin_automation.py — entry point.

Each tick (every 15 min via GitHub Actions):
  1) Sync engagement metrics for posted rows (likes/comments -> reach_score).
  2) Process replies on any in-flight Slack drafts (approve/reject/regenerate).
  3) If today's daily-draft hour has passed and we haven't drafted yet, generate one.

Cross-cutting concerns:
  - Top-level error alerting: any unhandled exception is sent to Slack as a
    critical error before the workflow goes red.
  - Idempotency: each draft has a UUID; mutating ops (LinkedIn POST, sheet
    status writes) check the registry before running.
  - Token expiry: monitor LinkedIn token age, alert at 5 days remaining.
  - Retries + circuit breakers: each adapter wraps its API calls.

Module map:
  config.py           env vars, constants, logger factory
  reliability.py      retries, circuit breakers, idempotency
  observability.py    Slack alerts, token expiry monitoring
  sheets.py           Google Sheets adapter (queue + _state tab)
  slack_helpers.py    Slack adapter (post drafts, read replies)
  linkedin_api.py     LinkedIn adapter (publish, fetch stats)
  generator.py        plan-write-critique generation pipeline
  engagement.py       feedback loop: sync stats, top-posts retrieval
  pipeline.py         the three phases above
"""

from __future__ import annotations

import sys

from anthropic import Anthropic
from slack_sdk import WebClient

from config import DRY_RUN, env_required_all, get_logger, is_staging, now_iso
from observability import alert_exception
from pipeline import (
    check_token_expiry,
    ensure_member_id,
    maybe_generate_daily_draft,
    process_pending_drafts,
    sync_engagement_metrics_phase,
)
from reliability import IdempotencyRegistry
from sheets import SheetClient
from slack_helpers import get_bot_user_id

LOG = get_logger("main")


def main() -> int:
    secrets = env_required_all()
    LOG.info("=== tick start at %s (env=%s, dry_run=%s) ===",
             now_iso(), "STAGING" if is_staging() else "PRODUCTION", DRY_RUN)

    # Adapters
    state = SheetClient.connect(secrets["SHEET_ID"], secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    slack = WebClient(token=secrets["SLACK_BOT_TOKEN"])
    anthropic_client = Anthropic(api_key=secrets["ANTHROPIC_API_KEY"])
    channel_id = secrets["SLACK_CHANNEL_ID"]
    linkedin_token = secrets["LINKEDIN_TOKEN"]

    # Idempotency registry on top of the _state tab
    registry = IdempotencyRegistry(state.state_get, state.state_set)
    registry.gc_old_keys(days=30)

    bot_user_id = get_bot_user_id(slack)

    # Resolve member ID and run token-expiry check. If the token is rejected
    # here we alert and exit early so downstream phases don't hammer LinkedIn.
    try:
        member_id = ensure_member_id(state, linkedin_token)
        check_token_expiry(state, slack, channel_id, member_id)
    except Exception as e:
        alert_exception(slack, channel_id, "ensure_member_id", e)
        raise

    # --- Phase 1: engagement sync ---
    try:
        sync_engagement_metrics_phase(state, slack, channel_id, linkedin_token)
    except Exception as e:
        alert_exception(slack, channel_id, "sync_engagement_metrics", e)
        # Non-fatal — continue to phase 2/3

    # --- Phase 2: process replies ---
    try:
        process_pending_drafts(
            state, slack, anthropic_client, channel_id, bot_user_id,
            linkedin_token, member_id, DRY_RUN, registry,
        )
    except Exception as e:
        alert_exception(slack, channel_id, "process_pending_drafts", e)

    # --- Phase 3: maybe today's draft ---
    try:
        maybe_generate_daily_draft(
            state, slack, anthropic_client, channel_id, registry,
        )
    except Exception as e:
        alert_exception(slack, channel_id, "maybe_generate_daily_draft", e)

    LOG.info("=== tick complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
