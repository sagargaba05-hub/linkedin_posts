"""
pipeline.py — three top-level phases per tick:

  Phase 1: sync_engagement_metrics_phase()
           Pull fresh likes/comments for posted rows, write reach_score back.

  Phase 2: process_pending_drafts()
           For each in-flight draft, check Slack thread; act on approve/reject/regenerate.
           Idempotency-protected: each draft has a UUID; the LinkedIn POST and the
           sheet status-write are only done once per (key, op) pair.

  Phase 3: maybe_generate_daily_draft()
           Once per day after the configured hour, pick the next pending row
           (or fall back to topics.md), pull top-performing past posts as
           few-shot context, generate a draft, post it to Slack.

Phase 1 runs every tick. Phase 2 runs every tick. Phase 3 runs at most once a day.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from anthropic import Anthropic
from slack_sdk import WebClient

from config import (
    ABANDONED_DRAFT_HOURS,
    get_logger,
    now_iso,
    now_local,
    today_str,
)
from engagement import (
    format_top_posts_for_prompt,
    load_top_performing_posts,
    sync_engagement_metrics,
)
from generator import (
    DraftRequest,
    generate_post,
    pick_fallback_topic,
)
from linkedin_api import LinkedInTokenRejected, get_member_id, publish_post, refresh_access_token
from observability import (
    alert_token_rejected,
    maybe_alert_token_expiry,
    record_token_first_seen,
)
from reliability import IdempotencyRegistry, new_idempotency_key
from runtime_config import get_daily_draft_hour, is_today_enabled, should_force_daily_draft
from sheets import SheetClient
from slack_helpers import (
    get_latest_user_reply,
    post_draft,
    post_followup,
    repost_in_thread,
)

LOG = get_logger("pipeline")

DRAFTS_STATE_MAX_CHARS = 45_000
DRAFTS_BULKY_FIELDS = {
    "critic_notes",
    "draft",
    "plan",
    "regen_history",
    "top_posts_block",
}
DRAFTS_KEEP_FULL_STATUSES = {"drafted", "posted"}
LINKEDIN_TOKEN_CACHE_KEY = "linkedin_access_token_cache"
LINKEDIN_TOKEN_REFRESH_BUFFER_MINUTES = 60


# --------------------------------------------------------------------------- #
# Member-ID bootstrap                                                          #
# --------------------------------------------------------------------------- #


def linkedin_refresh_configured(secrets: dict[str, str]) -> bool:
    return all(
        secrets.get(name)
        for name in ("LINKEDIN_REFRESH_TOKEN", "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET")
    )


def _cached_linkedin_token_is_valid(cache: dict) -> bool:
    try:
        expires_at = datetime.fromisoformat(cache["expires_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return expires_at > now_local() + timedelta(minutes=LINKEDIN_TOKEN_REFRESH_BUFFER_MINUTES)


def clear_linkedin_token_cache(state: SheetClient) -> None:
    state.state_set(LINKEDIN_TOKEN_CACHE_KEY, {})


def resolve_linkedin_token(
    state: SheetClient, secrets: dict[str, str], force_refresh: bool = False
) -> str:
    """Return a usable LinkedIn access token, refreshing it when configured."""
    if not linkedin_refresh_configured(secrets):
        return secrets["LINKEDIN_TOKEN"]

    cache = state.state_get(LINKEDIN_TOKEN_CACHE_KEY, {}) or {}
    if (
        not force_refresh
        and isinstance(cache, dict)
        and cache.get("access_token")
        and _cached_linkedin_token_is_valid(cache)
    ):
        LOG.info("Using cached refreshed LinkedIn access token")
        return cache["access_token"]

    refreshed = refresh_access_token(
        secrets["LINKEDIN_REFRESH_TOKEN"],
        secrets["LINKEDIN_CLIENT_ID"],
        secrets["LINKEDIN_CLIENT_SECRET"],
    )
    expires_in = int(refreshed.get("expires_in") or 0)
    expires_at = now_local() + timedelta(seconds=expires_in)
    state.state_set(
        LINKEDIN_TOKEN_CACHE_KEY,
        {
            "access_token": refreshed["access_token"],
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "refreshed_at": now_local().isoformat(timespec="seconds"),
            "scope": refreshed.get("scope", ""),
            "refresh_token_expires_in": refreshed.get("refresh_token_expires_in"),
        },
    )
    LOG.info("Refreshed LinkedIn access token; expires_at=%s", expires_at.isoformat())
    return refreshed["access_token"]


def ensure_member_id(state: SheetClient, token: str, validate_token: bool = False) -> str:
    cached = state.state_get("linkedin_member_id")
    if cached and not validate_token:
        LOG.info("Using cached LinkedIn member_id from state")
        record_token_first_seen(state.state_get, state.state_set, cached)
        return cached
    member_id = get_member_id(token)
    state.state_set("linkedin_member_id", member_id)
    record_token_first_seen(state.state_get, state.state_set, member_id)
    return member_id


# --------------------------------------------------------------------------- #
# Phase 1: engagement metrics                                                  #
# --------------------------------------------------------------------------- #


def sync_engagement_metrics_phase(
    state: SheetClient,
    slack: WebClient,
    channel_id: str,
    linkedin_token: str,
) -> None:
    """Pull fresh stats for all posted rows and update Reach score."""
    try:
        sync_engagement_metrics(state, linkedin_token)
    except LinkedInTokenRejected:
        alert_token_rejected(slack, channel_id)
        LOG.warning("LinkedIn token rejected during engagement sync; skipping sync this tick")
    except Exception:
        LOG.exception("sync_engagement_metrics failed (non-fatal)")


# --------------------------------------------------------------------------- #
# Phase 2: process replies                                                     #
# --------------------------------------------------------------------------- #


def _serialized_drafts_len(drafts: list[dict]) -> int:
    return len(json.dumps(drafts))


def _compact_draft_for_state(draft: dict, keep_full_text: bool) -> dict:
    compacted = dict(draft)
    compacted.pop("top_posts_block", None)
    if not keep_full_text:
        removed_bulky_state = False
        for key in DRAFTS_BULKY_FIELDS:
            if key in compacted:
                removed_bulky_state = True
                compacted.pop(key)
        if removed_bulky_state and not compacted.get("compacted_at"):
            compacted["compacted_at"] = now_iso()
    return compacted


def compact_drafts_for_state(
    drafts: list[dict], max_chars: int = DRAFTS_STATE_MAX_CHARS
) -> list[dict]:
    """Keep active/postable state useful while respecting Sheets' 50k cell cap."""
    compacted = [
        _compact_draft_for_state(d, d.get("status") in DRAFTS_KEEP_FULL_STATUSES) for d in drafts
    ]
    if _serialized_drafts_len(compacted) <= max_chars:
        return compacted

    # If posted history grows too large, preserve active Slack drafts first and
    # compact older posted records until the state cell is safely writable.
    for i, draft in enumerate(compacted):
        if draft.get("status") == "posted":
            compacted[i] = _compact_draft_for_state(draft, keep_full_text=False)
            if _serialized_drafts_len(compacted) <= max_chars:
                return compacted
    return compacted


def _save_drafts_state(state: SheetClient, drafts: list[dict]) -> None:
    compacted = compact_drafts_for_state(drafts)
    serialized_len = _serialized_drafts_len(compacted)
    if serialized_len > DRAFTS_STATE_MAX_CHARS:
        raise ValueError(
            f"Compacted drafts state is still too large for Google Sheets "
            f"({serialized_len} chars > {DRAFTS_STATE_MAX_CHARS})"
        )
    if compacted != drafts:
        LOG.info(
            "Compacted drafts state before save: %d -> %d chars",
            _serialized_drafts_len(drafts),
            serialized_len,
        )
    state.state_set("drafts", compacted)


def _gc_abandoned_drafts(drafts: list[dict]) -> list[dict]:
    cutoff = now_local() - timedelta(hours=ABANDONED_DRAFT_HOURS)
    abandoned = []
    for d in drafts:
        if d.get("status") != "drafted":
            continue
        drafted_at_str = d.get("drafted_at", "")
        if not drafted_at_str:
            continue
        try:
            drafted_at = datetime.fromisoformat(drafted_at_str)
        except ValueError:
            continue
        if drafted_at < cutoff:
            LOG.info(
                "Auto-abandoning stale draft sno=%s (drafted %s)", d.get("sno"), drafted_at_str
            )
            d["status"] = "abandoned"
            abandoned.append(d)
    return abandoned


def _sync_abandoned_rows(state: SheetClient, abandoned_drafts: list[dict]) -> None:
    for draft in abandoned_drafts:
        row_number = draft.get("row_number")
        if not row_number:
            continue
        try:
            state.update_row(row_number, {"status": "abandoned"})
            state.append_to_notes(row_number, "Auto-abandoned after stale Slack draft.")
        except Exception:
            LOG.exception("Failed to sync abandoned status for row=%s", row_number)


def process_pending_drafts(
    state: SheetClient,
    slack: WebClient,
    anthropic_client: Anthropic,
    channel_id: str,
    bot_user_id: str,
    linkedin_token: str,
    member_id: str,
    dry_run: bool,
    registry: IdempotencyRegistry,
) -> None:
    drafts: list[dict] = state.state_get("drafts", [])
    LOG.info("State holds %d total drafts", len(drafts))
    for i, d in enumerate(drafts):
        LOG.info(
            "  drafts[%d] sno=%s status=%s thread_ts=%s key=%s",
            i,
            d.get("sno"),
            d.get("status"),
            d.get("thread_ts"),
            d.get("idempotency_key", "(none)"),
        )
    if not drafts:
        return

    abandoned_drafts = _gc_abandoned_drafts(drafts)
    if abandoned_drafts:
        _sync_abandoned_rows(state, abandoned_drafts)
    changed = bool(abandoned_drafts)

    for d in drafts:
        if d.get("status") != "drafted":
            LOG.info("Skipping SNo. %s (status=%s)", d.get("sno"), d.get("status"))
            continue

        # Migrate older drafts that pre-date idempotency keys
        if not d.get("idempotency_key"):
            d["idempotency_key"] = new_idempotency_key()
            LOG.info("Backfilled idempotency_key for legacy draft sno=%s", d["sno"])
            changed = True

        thread_ts = d["thread_ts"]
        LOG.info("Checking SNo. %s thread for replies", d.get("sno"))
        reply = get_latest_user_reply(slack, channel_id, thread_ts, bot_user_id)
        if not reply:
            LOG.info("No actionable reply for SNo. %s", d.get("sno"))
            continue
        reply_lc = reply.lower().strip()
        LOG.info("Processing reply for SNo. %s: %r", d.get("sno"), reply_lc[:80])

        if reply_lc.startswith("approve"):
            _handle_approve(
                d, state, slack, channel_id, thread_ts, linkedin_token, member_id, dry_run, registry
            )
            changed = True
        elif reply_lc.startswith("reject"):
            _handle_reject(d, state, slack, channel_id, thread_ts, registry)
            changed = True
        elif reply_lc.startswith("regenerate"):
            feedback = reply.split(":", 1)[1].strip() if ":" in reply else ""
            _handle_regenerate(d, state, slack, anthropic_client, channel_id, thread_ts, feedback)
            changed = True
        else:
            if not d.get("nudged"):
                post_followup(
                    slack,
                    channel_id,
                    thread_ts,
                    "I didn't recognize that. Please reply with `approve`, "
                    "`reject`, or `regenerate: <feedback>`.",
                )
                d["nudged"] = True
                changed = True

    if changed:
        _save_drafts_state(state, drafts)


def _handle_approve(
    d: dict,
    state: SheetClient,
    slack: WebClient,
    channel_id: str,
    thread_ts: str,
    linkedin_token: str,
    member_id: str,
    dry_run: bool,
    registry: IdempotencyRegistry,
) -> None:
    key = d["idempotency_key"]
    LOG.info("SNo. %s approved — publishing (key=%s)", d["sno"], key)

    # Idempotency: skip the LinkedIn POST if we already did it for this key
    if registry.has_completed(key, "linkedin_publish"):
        LOG.warning("Already published for key=%s — skipping duplicate POST", key)
        post_followup(
            slack,
            channel_id,
            thread_ts,
            ":information_source: This draft was already published to LinkedIn — skipping duplicate.",
        )
        d["status"] = "posted"
        return

    try:
        if dry_run:
            post_url = "https://example.com/dry-run-post"
            LOG.info("[DRY_RUN] Skipping LinkedIn POST")
        else:
            post_url, _urn = publish_post(linkedin_token, member_id, d["draft"])
        registry.mark_completed(key, "linkedin_publish")

        d["status"] = "posted"
        d["post_url"] = post_url
        d["posted_at"] = now_iso()

        # Idempotency: only update sheet status once
        if not registry.has_completed(key, "sheet_status_posted"):
            updates = {"status": "posted", "post_url": post_url}
            if not d.get("is_auto") and not d.get("generated_by_written"):
                updates["generated_by"] = "Sagar"
                d["generated_by_written"] = True
            state.update_row(d["row_number"], updates)
            state.append_to_notes(d["row_number"], f"Posted to LinkedIn: {post_url}")
            registry.mark_completed(key, "sheet_status_posted")

        post_followup(
            slack,
            channel_id,
            thread_ts,
            f":white_check_mark: Posted to LinkedIn: {post_url}",
        )
    except LinkedInTokenRejected:
        from observability import alert_token_rejected

        alert_token_rejected(slack, channel_id)
        post_followup(
            slack,
            channel_id,
            thread_ts,
            ":x: LinkedIn token expired. Will retry once you've rotated the token.",
        )
    except Exception as e:
        LOG.exception("LinkedIn publish failed for SNo. %s", d["sno"])
        post_followup(
            slack,
            channel_id,
            thread_ts,
            f":x: LinkedIn post failed: `{e}`. Will retry on next run.",
        )


def _handle_reject(
    d: dict,
    state: SheetClient,
    slack: WebClient,
    channel_id: str,
    thread_ts: str,
    registry: IdempotencyRegistry,
) -> None:
    LOG.info("SNo. %s rejected", d["sno"])
    d["status"] = "rejected"
    key = d["idempotency_key"]
    if not registry.has_completed(key, "sheet_status_rejected"):
        state.update_row(d["row_number"], {"status": "rejected"})
        state.append_to_notes(d["row_number"], "Rejected via Slack — skipped.")
        registry.mark_completed(key, "sheet_status_rejected")
    post_followup(
        slack,
        channel_id,
        thread_ts,
        ":no_entry_sign: Marked as rejected. Skipping.",
    )


def _handle_regenerate(
    d: dict,
    state: SheetClient,
    slack: WebClient,
    anthropic_client: Anthropic,
    channel_id: str,
    thread_ts: str,
    feedback: str,
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
        # Use the same top-posts context the original generation had
        top_posts = load_top_performing_posts(state, state.state_get)
        top_block = format_top_posts_for_prompt(top_posts)
        from generator import generate_post as _gen

        result = _gen(anthropic_client, req, top_posts_block=top_block)
        d["draft"] = result.draft
        history = d.get("regen_history", [])
        history.append({"at": now_iso(), "feedback": feedback})
        d["regen_history"] = history
        repost_in_thread(slack, channel_id, thread_ts, d["sno"], result.draft)
        state.append_to_notes(d["row_number"], f"Regenerate requested. Feedback: {feedback}")
    except Exception as e:
        LOG.exception("Regeneration failed")
        post_followup(
            slack,
            channel_id,
            thread_ts,
            f":x: Regeneration failed: `{e}`. Try `regenerate: <new feedback>` again.",
        )


# --------------------------------------------------------------------------- #
# Phase 3: maybe generate today's draft                                       #
# --------------------------------------------------------------------------- #


def _effective_sno(row: dict) -> str:
    if row.get("sno"):
        return row["sno"]
    return f"row-{row.get('row_number', 'unknown')}"


def _select_next_pending(
    pending_rows: list[dict],
    in_flight_snos: set,
) -> dict | None:
    eligible = []
    for r in pending_rows:
        if not r.get("topic", "").strip():
            LOG.info("Skipping row=%s — empty Topic", r.get("row_number"))
            continue
        eff_sno = _effective_sno(r)
        if eff_sno in in_flight_snos:
            LOG.info("Skipping sno=%s — already in flight", eff_sno)
            continue
        r["sno"] = eff_sno
        eligible.append(r)

    if not eligible:
        return None

    def date_key(r: dict) -> tuple:
        from datetime import date

        try:
            d = r["date"]
            if d:
                from dateutil import parser as dateparser  # type: ignore

                parsed = dateparser.parse(d, dayfirst=False).date()
                return (0, parsed, r.get("row_number", 0))
        except Exception:
            pass
        return (1, date.max, r.get("row_number", 0))

    try:
        eligible.sort(key=date_key)
    except Exception as e:
        LOG.warning("Pending-row sort failed (%s); falling back to sheet order", e)

    pick = eligible[0]
    LOG.info(
        "Selected sno=%s row=%s Date=%r from %d eligible pending rows",
        pick["sno"],
        pick["row_number"],
        pick["date"],
        len(eligible),
    )
    return pick


def maybe_generate_daily_draft(
    state: SheetClient,
    slack: WebClient,
    anthropic_client: Anthropic,
    channel_id: str,
    registry: IdempotencyRegistry,
) -> None:
    now = now_local()
    today = today_str()
    force_daily_draft = should_force_daily_draft()

    # Runtime-config gating (settable via the local control panel)
    if not force_daily_draft and not is_today_enabled():
        return  # is_today_enabled() logs the reason
    daily_hour = get_daily_draft_hour()

    last_drafted_date = state.state_get("last_drafted_date", "")
    if not force_daily_draft and last_drafted_date == today:
        LOG.info("Daily draft already generated for %s — skipping", today)
        return
    if not force_daily_draft and now.hour < daily_hour:
        LOG.info("Too early (hour=%d, threshold=%d) — skipping daily draft", now.hour, daily_hour)
        return

    # Idempotency: also guard the "I drafted today" flag with the registry,
    # so two ticks racing can't both decide to generate.
    today_op = f"daily_draft_{today}"
    if not force_daily_draft and registry.has_completed("daily", today_op):
        LOG.info("daily_draft_%s already completed (idempotency) — skipping", today)
        # And ensure last_drafted_date is set in case state got out of sync
        state.state_set("last_drafted_date", today)
        return

    LOG.info("Generating daily draft for %s", today)

    pending = state.fetch_pending_rows()
    drafts: list[dict] = state.state_get("drafts", [])
    in_flight = {d["sno"] for d in drafts if d.get("status") in ("drafted", "posted")}

    # Pull engagement-feedback few-shots
    top_posts = load_top_performing_posts(state, state.state_get)
    top_block = format_top_posts_for_prompt(top_posts)

    pick = _select_next_pending(pending, in_flight)

    if pick:
        _draft_from_row(pick, state, slack, anthropic_client, channel_id, drafts, now, top_block)
    else:
        _draft_from_fallback_theme(
            state, slack, anthropic_client, channel_id, drafts, now, top_block
        )

    _save_drafts_state(state, drafts)
    state.state_set("last_drafted_date", today)
    registry.mark_completed("daily", today_op)


def _draft_from_row(
    pick: dict,
    state: SheetClient,
    slack: WebClient,
    anthropic_client: Anthropic,
    channel_id: str,
    drafts: list[dict],
    now,
    top_block: str,
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
    result = generate_post(anthropic_client, req, top_posts_block=top_block)
    thread_ts = post_draft(
        slack,
        channel_id,
        pick["sno"],
        result.draft,
        critic_verdict=result.critic_verdict,
        critic_notes=result.critic_notes,
        revision_count=result.revision_count,
    )

    drafts.append(
        {
            "sno": pick["sno"],
            "row_number": pick["row_number"],
            "thread_ts": thread_ts,
            "idempotency_key": new_idempotency_key(),
            "draft": result.draft,
            "plan": result.plan,
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
            "critic_verdict": result.critic_verdict,
        }
    )

    updates = {"status": "drafted"}
    if not pick.get("generated_by"):
        updates["generated_by"] = "Sagar"
    state.update_row(pick["row_number"], updates)
    state.append_to_notes(
        pick["row_number"],
        f"Drafted (critic={result.critic_verdict}, revisions={result.revision_count}). "
        f"Slack thread {thread_ts}",
    )


def _draft_from_fallback_theme(
    state: SheetClient,
    slack: WebClient,
    anthropic_client: Anthropic,
    channel_id: str,
    drafts: list[dict],
    now,
    top_block: str,
) -> None:
    LOG.info("No pending rows — using topics.md fallback")
    used_recently = state.state_get("recent_fallback_themes", [])
    theme = pick_fallback_topic(used_recently)
    used_recently = ([theme] + used_recently)[:10]
    state.state_set("recent_fallback_themes", used_recently)

    req = DraftRequest(topic=theme, voice="thoughtful")
    result = generate_post(anthropic_client, req, top_posts_block=top_block)

    auto_sno = f"auto-{today_str()}"
    try:
        row_number = state.append_auto_row(
            sno=auto_sno,
            topic=theme,
            voice="thoughtful",
            status="drafted",
        )
    except Exception:
        LOG.exception("Could not append auto row to queue")
        row_number = 0

    thread_ts = post_draft(
        slack,
        channel_id,
        auto_sno,
        result.draft,
        critic_verdict=result.critic_verdict,
        critic_notes=result.critic_notes,
        revision_count=result.revision_count,
    )

    drafts.append(
        {
            "sno": auto_sno,
            "row_number": row_number,
            "thread_ts": thread_ts,
            "idempotency_key": new_idempotency_key(),
            "draft": result.draft,
            "plan": result.plan,
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
            "critic_verdict": result.critic_verdict,
        }
    )

    if row_number:
        state.append_to_notes(
            row_number,
            f"Auto-generated by Cowork (no pending rows). "
            f"Critic={result.critic_verdict}, revisions={result.revision_count}. "
            f"Slack thread {thread_ts}",
        )


# --------------------------------------------------------------------------- #
# Token expiry monitor                                                        #
# --------------------------------------------------------------------------- #


def check_token_expiry(
    state: SheetClient,
    slack: WebClient,
    channel_id: str,
    member_id: str,
) -> None:
    try:
        maybe_alert_token_expiry(slack, channel_id, state.state_get, state.state_set, member_id)
    except Exception:
        LOG.exception("Token expiry check failed (non-fatal)")
