"""Tests for LinkedIn token rejection handling."""

import pipeline
from linkedin_api import LinkedInTokenRejected


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
