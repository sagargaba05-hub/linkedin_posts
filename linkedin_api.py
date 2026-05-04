"""
linkedin_api.py — minimal client for LinkedIn's userinfo and ugcPosts endpoints.
"""

from __future__ import annotations

import json
import requests

from config import LINKEDIN_UGC_POSTS, LINKEDIN_USERINFO, get_logger

LOG = get_logger("linkedin")


def get_member_id(token: str) -> str:
    """Fetch the LinkedIn member ID (the 'sub' claim from OpenID userinfo)."""
    LOG.info("Fetching LinkedIn member ID via /v2/userinfo")
    r = requests.get(
        LINKEDIN_USERINFO,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code == 401:
        raise RuntimeError("LinkedIn token rejected (401). Token expired or invalid.")
    r.raise_for_status()
    data = r.json()
    sub = data.get("sub")
    if not sub:
        raise RuntimeError(f"LinkedIn userinfo response missing 'sub': {data}")
    LOG.info("Member ID resolved to %s", sub)
    return sub


def publish_post(token: str, member_id: str, text: str) -> str:
    """Publish a public text-only post on the user's behalf. Returns the post URL."""
    body = {
        "author": f"urn:li:person:{member_id}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    LOG.info("POST /v2/ugcPosts (text length=%d)", len(text))
    r = requests.post(
        LINKEDIN_UGC_POSTS,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn POST failed [{r.status_code}]: {r.text}")
    urn = r.headers.get("x-restli-id") or r.json().get("id", "")
    LOG.info("LinkedIn API response status=%s urn=%s", r.status_code, urn)
    if not urn:
        raise RuntimeError(f"LinkedIn POST returned no URN. Body: {r.text}")
    # The full URN works in feed-update URLs for both ugcPost and share types.
    return f"https://www.linkedin.com/feed/update/{urn}/"
