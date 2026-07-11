"""Tests for LinkedIn token rejection handling."""

from datetime import timedelta

import pipeline
from linkedin_api import LinkedInTokenRejected


class FakeState:
    def __init__(self, cache=None):
        self.values = {}
        if cache is not None:
            self.values[pipeline.LINKEDIN_TOKEN_CACHE_KEY] = cache

    def state_get(self, key, default=None):
        return self.values.get(key, default)

    def state_set(self, key, value):
        self.values[key] = value


def test_engagement_sync_token_rejection_alerts_without_reraising(monkeypatch):
    calls = []

    def reject_token(_state, _token):
        raise LinkedInTokenRejected("expired")

    monkeypatch.setattr(pipeline, "sync_engagement_metrics", reject_token)
    monkeypatch.setattr(
        pipeline,
        "alert_token_rejected",
        lambda slack, channel_id: calls.append((slack, channel_id)),
    )

    pipeline.sync_engagement_metrics_phase(
        state=object(),
        slack="slack-client",
        channel_id="C123",
        linkedin_token="expired-token",
    )

    assert calls == [("slack-client", "C123")]


def test_resolve_linkedin_token_uses_static_token_without_refresh_config():
    state = FakeState()
    secrets = {
        "LINKEDIN_TOKEN": "static-token",
        "LINKEDIN_REFRESH_TOKEN": "",
        "LINKEDIN_CLIENT_ID": "",
        "LINKEDIN_CLIENT_SECRET": "",
    }

    assert pipeline.resolve_linkedin_token(state, secrets) == "static-token"


def test_resolve_linkedin_token_uses_valid_cached_refresh_token():
    state = FakeState(
        {
            "access_token": "cached-token",
            "expires_at": (pipeline.now_local() + timedelta(days=1)).isoformat(),
        }
    )
    secrets = {
        "LINKEDIN_TOKEN": "static-token",
        "LINKEDIN_REFRESH_TOKEN": "refresh-token",
        "LINKEDIN_CLIENT_ID": "client-id",
        "LINKEDIN_CLIENT_SECRET": "client-secret",
    }

    assert pipeline.resolve_linkedin_token(state, secrets) == "cached-token"


def test_resolve_linkedin_token_refreshes_and_caches_new_token(monkeypatch):
    state = FakeState()
    secrets = {
        "LINKEDIN_TOKEN": "static-token",
        "LINKEDIN_REFRESH_TOKEN": "refresh-token",
        "LINKEDIN_CLIENT_ID": "client-id",
        "LINKEDIN_CLIENT_SECRET": "client-secret",
    }

    monkeypatch.setattr(
        pipeline,
        "refresh_access_token",
        lambda refresh_token, client_id, client_secret: {
            "access_token": "new-token",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
            "scope": "openid profile w_member_social",
        },
    )

    assert pipeline.resolve_linkedin_token(state, secrets) == "new-token"
    assert state.values[pipeline.LINKEDIN_TOKEN_CACHE_KEY]["access_token"] == "new-token"
