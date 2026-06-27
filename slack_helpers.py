"""
slack_helpers.py — posting drafts, reading thread replies, follow-ups.
All API calls go through retry + circuit-breaker decorators.
"""

from __future__ import annotations

from slack_sdk import WebClient

from config import get_logger, is_staging
from reliability import slack_breaker, with_circuit, with_http_retries

LOG = get_logger("slack")


def _staging_prefix() -> str:
    return "[STAGING] " if is_staging() else ""


@with_circuit(slack_breaker)
@with_http_retries
def post_draft(
    client: WebClient,
    channel_id: str,
    sno: str,
    draft: str,
    critic_verdict: str = "PASS",
    critic_notes: str = "",
    revision_count: int = 0,
) -> str:
    """Post a new draft message. Returns the thread_ts (timestamp of the parent).
    Posts are text-only — no image suggestion (kept the approve→post path frictionless)."""
    prefix = _staging_prefix()
    parts = [f":rocket: *{prefix}Today's LinkedIn Draft (SNo. {sno})*", "", draft, "", "---"]

    if critic_verdict == "PASS":
        verdict_line = ":white_check_mark: *Critic verdict:* PASS"
    elif critic_verdict == "REVISE":
        verdict_line = ":warning: *Critic verdict:* REVISE — auto-revised once"
    else:
        verdict_line = ":x: *Critic verdict:* FAIL — auto-revised once but issues may remain"
    if revision_count > 0:
        verdict_line += f" (revised {revision_count}×)"
    parts.append(verdict_line)
    if critic_notes and critic_verdict != "PASS":
        parts.append(f"_{critic_notes}_")
    parts.append("")

    parts.extend(
        [
            "Reply *in this thread* with one of:",
            "• `approve`",
            "• `reject`",
            "• `regenerate: <your feedback>`",
            "",
            ':bulb: To reply in-thread: hover the message → click the speech-bubble "Reply in thread" icon.',
        ]
    )
    text = "\n".join(parts)
    resp = client.chat_postMessage(channel=channel_id, text=text)
    LOG.info(
        "Posted draft for sno=%s thread_ts=%s critic=%s revisions=%d",
        sno,
        resp["ts"],
        critic_verdict,
        revision_count,
    )
    return resp["ts"]


@with_circuit(slack_breaker)
@with_http_retries
def repost_in_thread(
    client: WebClient,
    channel_id: str,
    thread_ts: str,
    sno: str,
    draft: str,
) -> None:
    text = (
        f":arrows_counterclockwise: *{_staging_prefix()}Regenerated draft (SNo. {sno})*\n\n"
        f"{draft}\n\n"
        f"---\n"
        f"Reply with `approve`, `reject`, or `regenerate: <feedback>`."
    )
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
    LOG.info("Reposted regenerated draft in thread %s", thread_ts)


@with_circuit(slack_breaker)
@with_http_retries
def get_latest_user_reply(
    client: WebClient,
    channel_id: str,
    thread_ts: str,
    bot_user_id: str,
) -> str | None:
    """Return text of the latest non-bot reply in the thread, or None."""
    from slack_sdk.errors import SlackApiError

    try:
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=50)
    except SlackApiError as e:
        LOG.warning(
            "conversations_replies failed for ts=%s: %s", thread_ts, e.response.get("error")
        )
        return None

    msgs = resp.get("messages", [])
    LOG.info("Thread %s has %d total messages", thread_ts, len(msgs))
    for i, m in enumerate(msgs):
        LOG.info(
            "  msg[%d] user=%s bot_id=%s subtype=%s text=%r",
            i,
            m.get("user"),
            m.get("bot_id"),
            m.get("subtype"),
            (m.get("text") or "")[:60],
        )

    user_replies = []
    for m in msgs[1:]:
        if m.get("user") == bot_user_id:
            continue
        if m.get("bot_id"):
            continue
        if not m.get("text"):
            continue
        user_replies.append(m)
    LOG.info("Filtered to %d candidate user replies", len(user_replies))
    if not user_replies:
        return None
    latest = user_replies[-1].get("text", "").strip()
    LOG.info("Latest user reply text: %r", latest)
    return latest


@with_circuit(slack_breaker)
@with_http_retries
def post_followup(client: WebClient, channel_id: str, thread_ts: str, text: str) -> None:
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
    LOG.info("Posted follow-up in thread %s: %s", thread_ts, text[:80])


@with_circuit(slack_breaker)
@with_http_retries
def get_bot_user_id(client: WebClient) -> str:
    resp = client.auth_test()
    bot_id = resp["user_id"]
    LOG.info("Bot user_id resolved to %s", bot_id)
    return bot_id
