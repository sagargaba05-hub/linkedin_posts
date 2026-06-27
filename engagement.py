"""
engagement.py — the feedback loop that turns "I posted some stuff" into
"the post-generator gets smarter every week."

Two functions:

1) sync_engagement_metrics(): for every row with status=posted and a post URL,
   call LinkedIn's socialActions endpoint to fetch likes + comments. Compute
   reach_score = likes + 2*comments and write it back to the Reach score column.
   Idempotency-safe via a per-row last-synced timestamp in state, but we still
   re-sync on every tick because engagement keeps changing for ~24-48h.

2) load_top_performing_posts(): returns the top-N posts by reach score, with
   their actual text, for the generator to use as few-shot examples. The system
   prompt then says: "Here are posts of yours that performed well; emulate
   their structure where appropriate." This is implicit RLHF — the model
   biases toward what your audience actually engages with.
"""

from __future__ import annotations

from typing import Any

from config import (
    TOP_POST_MIN_REACH,
    TOP_POSTS_FEW_SHOT_COUNT,
    get_logger,
)
from linkedin_api import (
    LinkedInTokenRejected,
    extract_urn_from_post_url,
    fetch_post_stats,
)
from sheets import SheetClient

LOG = get_logger("engagement")


def sync_engagement_metrics(
    state: SheetClient,
    linkedin_token: str,
) -> dict[str, int]:
    """For every posted row, fetch fresh stats from LinkedIn and update the
    Reach score column. Returns a summary dict {synced, errored, skipped}."""
    posted = state.fetch_posted_rows()
    if not posted:
        LOG.info("No posted rows to sync")
        return {"synced": 0, "errored": 0, "skipped": 0}

    summary = {"synced": 0, "errored": 0, "skipped": 0}
    for row in posted:
        urn = extract_urn_from_post_url(row["post_url"])
        if not urn:
            LOG.info(
                "Row %s: post_url=%r doesn't contain a URN — skipping",
                row["row_number"],
                row["post_url"][:60],
            )
            summary["skipped"] += 1
            continue
        try:
            stats = fetch_post_stats(linkedin_token, urn)
        except LinkedInTokenRejected:
            # Re-raise so caller can alert + stop
            raise
        except Exception as e:
            LOG.warning("Stats fetch errored for row %s urn=%s: %s", row["row_number"], urn, e)
            summary["errored"] += 1
            continue

        if stats.get("error"):
            summary["errored"] += 1
            continue

        new_score = str(stats["reach_score"])
        existing_score = (row.get("reach_score") or "").strip()
        if existing_score == new_score:
            LOG.info("Row %s: score unchanged (%s) — skipping write", row["row_number"], new_score)
            summary["skipped"] += 1
            continue

        try:
            state.update_row(row["row_number"], {"reach_score": new_score})
            summary["synced"] += 1
            LOG.info(
                "Row %s: reach_score %s -> %s (likes=%s, comments=%s)",
                row["row_number"],
                existing_score or "(blank)",
                new_score,
                stats["likes"],
                stats["comments"],
            )
        except Exception as e:
            LOG.warning("Sheet write failed for row %s: %s", row["row_number"], e)
            summary["errored"] += 1

    LOG.info("Engagement sync summary: %s", summary)
    return summary


def load_top_performing_posts(
    state: SheetClient,
    state_get,
    limit: int = TOP_POSTS_FEW_SHOT_COUNT,
    min_reach: int = TOP_POST_MIN_REACH,
) -> list[dict[str, Any]]:
    """Return up to `limit` past posts with reach_score >= min_reach, sorted
    descending. Each entry has keys: topic, draft, reach_score, post_url.
    Used as few-shot examples in the generator's system prompt."""
    posted = state.fetch_posted_rows()

    # Pull the actual draft text from the state's drafts list
    drafts: list[dict] = state_get("drafts", [])
    drafts_by_row = {d.get("row_number"): d for d in drafts if d.get("row_number")}

    candidates = []
    for row in posted:
        try:
            score = int((row.get("reach_score") or "0").strip() or 0)
        except ValueError:
            score = 0
        if score < min_reach:
            continue
        d = drafts_by_row.get(row["row_number"])
        if not d or not d.get("draft"):
            continue
        candidates.append(
            {
                "topic": row.get("topic") or d.get("topic", ""),
                "draft": d["draft"],
                "reach_score": score,
                "post_url": row.get("post_url", ""),
            }
        )

    candidates.sort(key=lambda c: c["reach_score"], reverse=True)
    top = candidates[:limit]
    LOG.info(
        "Loaded %d top-performing past posts (min_reach=%d, limit=%d)", len(top), min_reach, limit
    )
    return top


def format_top_posts_for_prompt(top_posts: list[dict[str, Any]]) -> str:
    """Format top posts as a readable block for the system prompt."""
    if not top_posts:
        return ""
    lines = [
        "PAST POSTS THAT PERFORMED WELL — emulate their structure, hook style, "
        "and tone where appropriate. These reflect what your audience actually "
        "engages with:\n",
    ]
    for i, p in enumerate(top_posts, start=1):
        lines.append(f"--- Top post #{i} (reach_score={p['reach_score']}, topic: {p['topic']}) ---")
        lines.append(p["draft"])
        lines.append("")
    return "\n".join(lines)
