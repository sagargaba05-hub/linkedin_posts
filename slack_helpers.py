"""
slack_helpers.py — Posting drafts, reading thread replies, posting follow-ups.
"""

from __future__ import annotations

from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import get_logger

LOG = get_logger("slack")


def post_draft(client: WebClient, channel_id: str, sno: str, draft: str) -> str:
    """Post a new draft message. Returns the thread_ts (timestamp of the parent)."""
    text = (
        f":rocket: *Today's LinkedIn Draft (SNo. {sno})*\n\n"
        f"{draft}\n\n"
        f"---\n"
        f"Reply *in this thread* with one of:\n"
        f"• `approve`\n"
        f"• `reject`\n"
        f"• `regenerate: <your feedback>`\n"
        f"\n"
        f":bulb: To reply in-thread: hover the message → click the speech-bubble \"Reply in thread\" icon."
    )
    resp = client.chat_postMessage(channel=channel_id, text=text)
    LOG.info("Posted draft for sno=%s thread_ts=%s", sno, resp["ts"])
    return resp["ts"]


def repost_in_thread(
    client: WebClient, channel_id: str, thread_ts: str, sno: str, draft: str
) -> None:
    text = (
        f":arrows_counterclockwise: *Regenerated draft (SNo. {sno})*\n\n"
        f"{draft}\n\n"
        f"---\n"
        f"Reply with `approve`, `reject`, or `regenerate: <feedback>`."
    )
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
    LOG.info("Reposted regenerated draft in thread %s", thread_ts)


def get_latest_user_reply(
    client: WebClient, channel_id: str, thread_ts: str, bot_user_id: str
) -> Optional[str]:
    """Return text of the latest non-bot reply in the thread, or None.
    Verbose logs let us see what's in the thread when something looks wrong."""
    try:
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=50)
    except SlackApiError as e:
        LOG.warning("conversations_replies failed for ts=%s: %s",
                    thread_ts, e.response.get("error"))
        return None

    msgs = resp.get("messages", [])
    LOG.info("Thread %s has %d total messages", thread_ts, len(msgs))
    for i, m in enumerate(msgs):
        LOG.info(
            "  msg[%d] user=%s bot_id=%s subtype=%s text=%r",
            i, m.get("user"), m.get("bot_id"), m.get("subtype"),
            (m.get("text") or "")[:60],
        )

    user_replies = []
    for m in msgs[1:]:  # skip parent
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


def post_followup(client: WebClient, channel_id: str, thread_ts: str, text: str) -> None:
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
    LOG.info("Posted follow-up in thread %s: %s", thread_ts, text[:80])


def get_bot_user_id(client: WebClient) -> str:
    resp = client.auth_test()
    bot_id = resp["user_id"]
    LOG.info("Bot user_id resolved to %s", bot_id)
    return bot_id
