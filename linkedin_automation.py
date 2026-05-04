"""
linkedin_automation.py — entry point.

Each tick (every 15 min via GitHub Actions):
  1) Process replies on any in-flight Slack drafts (approve/reject/regenerate).
  2) If today's 10 AM draft hasn't fired yet, generate one.

All real work lives in the modules:
  config.py        env vars, constants, logger factory
  sheets.py        Google Sheets (queue tab + hidden _state tab)
  slack_helpers.py post drafts, read thread replies, follow-ups
  linkedin_api.py  publish to LinkedIn UGC posts API
  generator.py     Anthropic draft generation + prompt template loading
  pipeline.py      the two top-level phases above

User-editable inputs that influence post quality live in prompts/:
  about_me.md         who you are, what you care about
  voice_examples.md   posts you'd want to emulate / things to avoid
  topics.md           pool used by the AI-trend fallback when sheet is empty
"""

from __future__ import annotations

import sys

from anthropic import Anthropic
from slack_sdk import WebClient

from config import DRY_RUN, env_required_all, get_logger, now_iso
from pipeline import (
    ensure_member_id,
    maybe_generate_daily_draft,
    process_pending_drafts,
)
from sheets import SheetClient
from slack_helpers import get_bot_user_id

LOG = get_logger("main")


def main() -> int:
    secrets = env_required_all()
    LOG.info("=== tick start at %s ===", now_iso())
    if DRY_RUN:
        LOG.info("DRY_RUN enabled — LinkedIn POSTs will be skipped")

    state = SheetClient.connect(secrets["SHEET_ID"], secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    slack = WebClient(token=secrets["SLACK_BOT_TOKEN"])
    anthropic_client = Anthropic(api_key=secrets["ANTHROPIC_API_KEY"])

    bot_user_id = get_bot_user_id(slack)
    member_id = ensure_member_id(state, secrets["LINKEDIN_TOKEN"])

    # Phase 1: handle replies
    try:
        process_pending_drafts(
            state, slack, anthropic_client,
            secrets["SLACK_CHANNEL_ID"], bot_user_id,
            secrets["LINKEDIN_TOKEN"], member_id, DRY_RUN,
        )
    except Exception:
        LOG.exception("process_pending_drafts crashed")

    # Phase 2: maybe make today's draft
    try:
        maybe_generate_daily_draft(
            state, slack, anthropic_client, secrets["SLACK_CHANNEL_ID"],
        )
    except Exception:
        LOG.exception("maybe_generate_daily_draft crashed")

    LOG.info("=== tick complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
