"""
pipeline.py — the two top-level phases of each tick:

1) process_pending_drafts — for every draft that's been posted to Slack but not
   yet resolved, look at the thread and act on `approve` / `reject` /
   `regenerate: <feedback>` replies.

2) maybe_generate_daily_draft — once per day after the configured hour, pick the
   next pending row from the sheet (or fall back to a topics.md theme) and post
   a fresh draft to Slack.

All sheet-write logic lives here so the audit trail (Notes column updates) is
consistent across phases.
"""

from __future__ import annotations

from anthropic import Anthropic
from slack_sdk import WebClient

from config import DAILY_DRAFT_HOUR, get_logger, now_iso, now_local, today_str
from generator import DraftRequest, generate_draft, pick_fallback_topic
from linkedin_api import publish_post
from sheets import SheetClient
from slack_helpers import (
    get_latest_user_reply,
    post_draft,
    post_followup,
    repost_in_thread,
)

LOG = get_logger("pipeline")


# --------------------------------------------------------------------------- #
# Member-ID bootstrap                                                         #
# --------------------------------------------------------------------------- #


def ensure_member_id(state: SheetClient, token: str) -> str:
    cached = state.state_get("linkedin_member_id")
    if cached:
        LOG.info("Using cached LinkedIn member_id from state")
        return cached
    from linkedin_api import get_member_id  # late import to keep modules loose
    member_id = get_member_id(token)
    state.state_set("linkedin_member_id", member_id)
    return member_id


# --------------------------------------------------------------------------- #
# Phase 1: process replies                                                    #
# --------------------------------------------------------------------------- #


def process_pending_drafts(
    state: SheetClient,
    slack: WebClient,
    anthropic_client: Anthropic,
    channel_id: str,
    bot_user_id: str,
    linkedin_token: str,
    member_id: str,
    dry_run: bool,
) -> None:
    drafts: list[dict] = state.state_get("drafts", [])
    LOG.info("State holds %d total drafts", len(drafts))
    for i, d in enumerate(drafts):
        LOG.info("  drafts[%d] sno=%s status=%s thread_ts=%s",
                 i, d.get("sno"), d.get("status"), d.get("thread_ts"))
    if not drafts:
        return

    changed = False
    for d in drafts:
        if d.get("status") != "drafted":
            LOG.info("Skipping SNo. %s (status=%s)", d.get("sno"), d.get("status"))
            continue

        thread_ts = d["thread_ts"]
        LOG.info("Checking SNo. %s thread for replies", d.get("sno"))
        reply = get_latest_user_reply(slack, channel_id, thread_ts, bot_user_id)
        if not reply:
            LOG.info("No actionable reply for SNo. %s", d.get("sno"))
            continue
        reply_lc = reply.lower().strip()
        LOG.info("Processing reply for SNo. %s: %r", d.get("sno"), reply_lc[:80])

        if reply_lc.startswith("approve"):
            _handle_approve(d, state, slack, channel_id, thread_ts,
                            linkedin_token, member_id, dry_run)
            changed = True

        elif reply_lc.startswith("reject"):
            _handle_reject(d, state, slack, channel_id, thread_ts)
            changed = True

        elif reply_lc.startswith("regenerate"):
            feedback = reply.split(":", 1)[1].strip() if ":" in reply else ""
            _handle_regenerate(d, state, slack, anthropic_client,
                               channel_id, thread_ts, feedback)
            changed = True

        else:
            if not d.get("nudged"):
                post_followup(
                    slack, channel_id, thread_ts,
                    "I didn't recognize that. Please reply with `approve`, "
                    "`reject`, or `regenerate: <feedback>`.",
                )
                d["nudged"] = True
                changed = True

    if changed:
        state.state_set("drafts", drafts)


def _handle_approve(
    d: dict, state: SheetClient, slack: WebClient,
    channel_id: str, thread_ts: str,
    linkedin_token: str, member_id: str, dry_run: bool,
) -> None:
    LOG.info("SNo. %s approved — publishing", d["sno"])
    try:
        if dry_run:
            post_url = "https://example.com/dry-run-post"
            LOG.info("[DRY_RUN] Skipping LinkedIn POST")
        else:
            post_url = publish_post(linkedin_token, member_id, d["draft"])
        d["status"] = "posted"
        d["post_url"] = post_url
        d["posted_at"] = now_iso()
        # Write to sheet: status, post_url, generated_by (if not already set)
        updates = {"status": "posted", "post_url": post_url}
        if not d.get("is_auto") and not d.get("generated_by_written"):
            updates["generated_by"] = "Sagar"
            d["generated_by_written"] = True
        state.update_row(d["row_number"], updates)
        state.append_to_notes(d["row_number"], f"Posted to LinkedIn: {post_url}")
        post_followup(
            slack, channel_id, thread_ts,
            f":white_check_mark: Posted to LinkedIn: {post_url}",
        )
    except Exception as e:
        LOG.exception("LinkedIn publish failed for SNo. %s", d["sno"])
        post_followup(
            slack, channel_id, thread_ts,
            f":x: LinkedIn post failed: `{e}`. Will retry on next run.",
        )


def _handle_reject(
    d: dict, state: SheetClient, slack: WebClient,
    channel_id: str, thread_ts: str,
) -> None:
    LOG.info("SNo. %s rejected", d["sno"])
    d["status"] = "rejected"
    state.update_row(d["row_number"], {"status": "rejected"})
    state.append_to_notes(d["row_number"], "Rejected via Slack — skipped.")
    post_followup(
        slack, channel_id, thread_ts,
        ":no_entry_sign: Marked as rejected. Skipping.",
    )


def _handle_regenerate(
    d: dict, state: SheetClient, slack: WebClient, anthropic_client: Anthropic,
    channel_id: str, thread_ts: str, feedback: str,
) -> None:
    LOG.info("SNo. %s regenerate requested. Feedback=%r", d["sno"], feedback[:80])
    req = DraftRequest(
        topic=d.get("topic", ""),
        angle=d.get("angle", ""),
        key_points=d.get("key_points", ""),
        voice=d.get("voice", ""),
        hook_style=d.get("hook_style", ""),
        link=d.get("link", ""),
        cta=d.get("cta", ""),
        feedback=feedback,
    )
    try:
        new_draft = generate_draft(anthropic_client, req)
        d["draft"] = new_draft
        history = d.get("regen_history", [])
        history.append({"at": now_iso(), "feedback": feedback})
        d["regen_history"] = history
        repost_in_thread(slack, channel_id, thread_ts, d["sno"], new_draft)
        state.append_to_notes(d["row_number"], f"Regenerate requested. Feedback: {feedback}")
    except Exception as e:
        LOG.exception("Regeneration failed")
        post_followup(
            slack, channel_id, thread_ts,
            f":x: Regeneration failed: `{e}`. Try `regenerate: <new feedback>` again.",
        )


# --------------------------------------------------------------------------- #
# Phase 2: maybe generate today's draft                                       #
# --------------------------------------------------------------------------- #


def _select_next_pending(
    pending_rows: list[dict], in_flight_snos: set,
) -> dict | None:
    """Pick the next row to draft. Preference order:
       1) earliest Date among pending rows (if Date is filled and parseable)
       2) lowest SNo. among pending rows
       Skips any SNo already in the in-flight set so we don't re-pick."""
    eligible = [r for r in pending_rows if r["sno"] and r["sno"] not in in_flight_snos]
    if not eligible:
        return None

    # Try date-first ordering
    def date_key(r: dict) -> tuple:
        from datetime import date
        try:
            d = r["date"]
            if d:
                # Accept YYYY-MM-DD or DD-MM-YYYY etc. — be lenient
                from dateutil import parser as dateparser  # type: ignore
                parsed = dateparser.parse(d, dayfirst=False).date()
                return (0, parsed, r["sno"])
        except Exception:
            pass
        # No date or unparseable — push to end, sort by SNo
        try:
            sno_int = int(r["sno"])
        except ValueError:
            sno_int = 10**9
        return (1, date.max, sno_int)

    try:
        eligible.sort(key=date_key)
    except Exception as e:
        LOG.warning("Pending-row sort failed (%s); falling back to sheet order", e)

    pick = eligible[0]
    LOG.info("Selected SNo. %s (Date=%r) from %d eligible pending rows",
             pick["sno"], pick["date"], len(eligible))
    return pick


def maybe_generate_daily_draft(
    state: SheetClient, slack: WebClient, anthropic_client: Anthropic, channel_id: str,
) -> None:
    now = now_local()
    today = today_str()
    last_drafted_date = state.state_get("last_drafted_date", "")
    if last_drafted_date == today:
        LOG.info("Daily draft already generated for %s — skipping", today)
        return
    if now.hour < DAILY_DRAFT_HOUR:
        LOG.info("Too early (hour=%d, threshold=%d) — skipping daily draft",
                 now.hour, DAILY_DRAFT_HOUR)
        return

    LOG.info("Generating daily draft for %s", today)

    pending = state.fetch_pending_rows()
    drafts: list[dict] = state.state_get("drafts", [])
    in_flight = {d["sno"] for d in drafts if d.get("status") in ("drafted", "posted")}

    pick = _select_next_pending(pending, in_flight)

    if pick:
        _draft_from_row(pick, state, slack, anthropic_client, channel_id, drafts, now)
    else:
        _draft_from_fallback_theme(state, slack, anthropic_client, channel_id, drafts, now)

    state.state_set("drafts", drafts)
    state.state_set("last_drafted_date", today)


def _draft_from_row(
    pick: dict, state: SheetClient, slack: WebClient, anthropic_client: Anthropic,
    channel_id: str, drafts: list[dict], now,
) -> None:
    LOG.info("Drafting from sheet row %d (SNo. %s)", pick["row_number"], pick["sno"])
    req = DraftRequest(
        topic=pick["topic"],
        angle=pick["angle"],
        key_points=pick["key_points"],
        voice=pick["voice"] or "thoughtful",
        hook_style=pick["hook_style"],
        link=pick["link"],
        cta=pick["cta"],
    )
    draft_text = generate_draft(anthropic_client, req)
    thread_ts = post_draft(slack, channel_id, pick["sno"], draft_text)

    drafts.append({
        "sno": pick["sno"],
        "row_number": pick["row_number"],
        "thread_ts": thread_ts,
        "draft": draft_text,
        "topic": pick["topic"],
        "angle": pick["angle"],
        "key_points": pick["key_points"],
        "voice": pick["voice"],
        "hook_style": pick["hook_style"],
        "link": pick["link"],
        "cta": pick["cta"],
        "status": "drafted",
        "drafted_at": now.isoformat(),
        "is_auto": False,
    })

    # Sheet writes for this draft
    updates = {"status": "drafted"}
    if not pick["generated_by"]:
        updates["generated_by"] = "Sagar"
    state.update_row(pick["row_number"], updates)
    state.append_to_notes(
        pick["row_number"],
        f"Draft generated and posted to Slack thread {thread_ts}",
    )


def _draft_from_fallback_theme(
    state: SheetClient, slack: WebClient, anthropic_client: Anthropic,
    channel_id: str, drafts: list[dict], now,
) -> None:
    LOG.info("No pending rows — using topics.md fallback")
    used_recently = state.state_get("recent_fallback_themes", [])
    theme = pick_fallback_topic(used_recently)
    used_recently = ([theme] + used_recently)[:10]  # remember last 10
    state.state_set("recent_fallback_themes", used_recently)

    req = DraftRequest(topic=theme, voice="thoughtful")
    draft_text = generate_draft(anthropic_client, req)

    auto_sno = f"auto-{today_str()}"
    try:
        row_number = state.append_auto_row(
            sno=auto_sno, topic=theme, voice="thoughtful", status="drafted",
        )
    except Exception:
        LOG.exception("Could not append auto row to queue")
        row_number = 0

    thread_ts = post_draft(slack, channel_id, auto_sno, draft_text)

    drafts.append({
        "sno": auto_sno,
        "row_number": row_number,
        "thread_ts": thread_ts,
        "draft": draft_text,
        "topic": theme,
        "angle": "",
        "key_points": "",
        "voice": "thoughtful",
        "hook_style": "",
        "link": "",
        "cta": "",
        "status": "drafted",
        "drafted_at": now.isoformat(),
        "is_auto": True,
    })

    if row_number:
        state.append_to_notes(
            row_number,
            f"Auto-generated by Cowork (no pending rows). Posted to Slack thread {thread_ts}",
        )
