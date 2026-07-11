"""
linkedin_api.py — LinkedIn REST client.

Three operations we use:
  - get_member_id()    : /v2/userinfo to resolve "urn:li:person:..." for posting
  - publish_post()     : /v2/ugcPosts to publish text content
  - fetch_post_stats() : /v2/socialActions/{urn} for engagement counts

All HTTP calls are wrapped with retry-on-transient + circuit breaker.
A 401 from the publish endpoint raises a special LinkedInTokenRejected so the
pipeline can alert via Slack and stop trying for the rest of the tick.
"""

from __future__ import annotations

import json

import requests

from config import (
    LINKEDIN_ACCESS_TOKEN_URL,
    LINKEDIN_SOCIAL_ACTIONS,
    LINKEDIN_UGC_POSTS,
    LINKEDIN_USERINFO,
    get_logger,
)
from reliability import linkedin_breaker, with_circuit, with_http_retries

LOG = get_logger("linkedin")


class LinkedInTokenRejected(Exception):
    """Raised on 401 from any LinkedIn endpoint. Caller should alert + stop."""


class LinkedInTokenRefreshFailed(Exception):
    """Raised when LinkedIn cannot refresh the access token."""


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #


@with_circuit(linkedin_breaker)
@with_http_retries
def get_member_id(token: str) -> str:
    """Fetch the member ID (the 'sub' claim from OpenID userinfo)."""
    LOG.info("GET /v2/userinfo")
    r = requests.get(
        LINKEDIN_USERINFO,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code == 401:
        raise LinkedInTokenRejected("LinkedIn token rejected on userinfo")
    r.raise_for_status()
    data = r.json()
    sub = data.get("sub")
    if not sub:
        raise RuntimeError(f"userinfo response missing 'sub': {data}")
    LOG.info("Member ID resolved to %s", sub)
    return sub


@with_circuit(linkedin_breaker)
@with_http_retries
def refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Exchange a LinkedIn refresh token for a fresh access token."""
    LOG.info("POST /oauth/v2/accessToken (grant_type=refresh_token)")
    r = requests.post(
        LINKEDIN_ACCESS_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if r.status_code >= 400:
        raise LinkedInTokenRefreshFailed(
            f"LinkedIn token refresh failed [{r.status_code}]: {r.text[:300]}"
        )
    data = r.json()
    if not data.get("access_token"):
        raise LinkedInTokenRefreshFailed("LinkedIn token refresh response missing access_token")
    return data


# --------------------------------------------------------------------------- #
# Publish                                                                     #
# --------------------------------------------------------------------------- #


@with_circuit(linkedin_breaker)
@with_http_retries
def publish_post(token: str, member_id: str, text: str) -> tuple[str, str]:
    """Publish a public text-only post. Returns (post_url, urn).
    The urn is what we need to fetch stats later via socialActions."""
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
    if r.status_code == 401:
        raise LinkedInTokenRejected("LinkedIn token rejected on publish")
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn POST failed [{r.status_code}]: {r.text}")
    urn = r.headers.get("x-restli-id") or r.json().get("id", "")
    LOG.info("Publish OK status=%s urn=%s", r.status_code, urn)
    if not urn:
        raise RuntimeError(f"LinkedIn POST returned no URN. Body: {r.text}")
    return f"https://www.linkedin.com/feed/update/{urn}/", urn


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #


@with_circuit(linkedin_breaker)
@with_http_retries
def fetch_post_stats(token: str, urn: str) -> dict:
    """Fetch like + comment counts for a post.
    Returns {'likes': int, 'comments': int, 'reach_score': int} where
    reach_score = likes + 2*comments (comments weighted more heavily).
    On 404 (post deleted) or other errors, returns zeros so the caller doesn't fail."""
    # The socialActions endpoint takes the URN-encoded ID
    url = f"{LINKEDIN_SOCIAL_ACTIONS}/{urn}"
    LOG.info("GET %s", url)
    try:
        r = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        LOG.warning("Stats fetch failed (will retry on next tick): %s", e)
        return {"likes": 0, "comments": 0, "reach_score": 0, "error": str(e)}

    if r.status_code == 401:
        raise LinkedInTokenRejected("LinkedIn token rejected on stats fetch")
    if r.status_code == 404:
        LOG.info("Post not found (likely deleted): %s", urn)
        return {"likes": 0, "comments": 0, "reach_score": 0, "deleted": True}
    if r.status_code >= 400:
        LOG.warning("Stats fetch returned %d: %s", r.status_code, r.text[:200])
        return {"likes": 0, "comments": 0, "reach_score": 0, "error": r.text[:200]}

    data = r.json()
    likes = (data.get("likesSummary") or {}).get("totalLikes", 0)
    # Sometimes the count is under different keys depending on API version
    if not likes:
        likes = (data.get("likesSummary") or {}).get("aggregatedTotalLikes", 0)
    comments = (data.get("commentsSummary") or {}).get("totalFirstLevelComments", 0)
    if not comments:
        comments = (data.get("commentsSummary") or {}).get("aggregatedTotalComments", 0)

    reach_score = int(likes) + 2 * int(comments)
    LOG.info("Stats for %s: likes=%s, comments=%s, score=%s", urn, likes, comments, reach_score)
    return {
        "likes": int(likes),
        "comments": int(comments),
        "reach_score": reach_score,
    }


def extract_urn_from_post_url(post_url: str) -> str | None:
    """Pull the URN out of a feed-update URL.
    Example: https://www.linkedin.com/feed/update/urn:li:share:7457.../
    -> urn:li:share:7457...
    Returns None if the URL doesn't look like a LinkedIn post URL."""
    if not post_url or "linkedin.com/feed/update/" not in post_url:
        return None
    after = post_url.split("/feed/update/", 1)[1]
    urn = after.split("/")[0].split("?")[0]
    if urn.startswith(("urn:li:share:", "urn:li:ugcPost:", "urn:li:activity:")):
        return urn
    return None
