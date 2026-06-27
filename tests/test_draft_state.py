"""Tests for compact draft state storage."""

import json
from types import SimpleNamespace

import pipeline


class FakeState:
    def __init__(self, drafts=None):
        self.saved = {}
        self.rows_updated = []
        self.notes = []
        self._drafts = drafts or []

    def state_get(self, key, default=None):
        if key == "drafts":
            return self._drafts
        if key == "last_drafted_date":
            return ""
        return default

    def state_set(self, key, value):
        self.saved[key] = value
        if key == "drafts":
            self._drafts = value

    def fetch_pending_rows(self):
        return [
            {
                "sno": "42",
                "row_number": 2,
                "topic": "AI reliability",
                "angle": "",
                "key_points": "",
                "voice": "thoughtful",
                "hook_style": "",
                "link": "",
                "cta": "",
                "generated_by": "",
                "date": "2026-06-27",
            }
        ]

    def update_row(self, row_number, updates):
        self.rows_updated.append((row_number, updates))

    def append_to_notes(self, row_number, note):
        self.notes.append((row_number, note))


class FakeRegistry:
    def __init__(self):
        self.marked = []

    def has_completed(self, key, op):
        return False

    def mark_completed(self, key, op):
        self.marked.append((key, op))


def test_compaction_keeps_active_draft_text_and_thread():
    drafts = [
        {
            "sno": "1",
            "thread_ts": "123.45",
            "status": "drafted",
            "draft": "full draft",
            "plan": "plan text",
            "top_posts_block": "large prompt context",
        }
    ]

    compacted = pipeline.compact_drafts_for_state(drafts)

    assert compacted[0]["draft"] == "full draft"
    assert compacted[0]["thread_ts"] == "123.45"
    assert compacted[0]["plan"] == "plan text"
    assert "top_posts_block" not in compacted[0]


def test_compaction_keeps_posted_draft_for_top_post_examples():
    drafts = [
        {
            "sno": "1",
            "status": "posted",
            "draft": "posted text",
            "post_url": "https://example.com/post",
            "top_posts_block": "large prompt context",
        }
    ]

    compacted = pipeline.compact_drafts_for_state(drafts)

    assert compacted[0]["draft"] == "posted text"
    assert compacted[0]["post_url"] == "https://example.com/post"
    assert "top_posts_block" not in compacted[0]


def test_compaction_strips_bulky_abandoned_fields_but_keeps_metadata():
    drafts = [
        {
            "sno": "old",
            "row_number": 9,
            "thread_ts": "999.1",
            "idempotency_key": "key",
            "status": "abandoned",
            "draft": "x" * 1000,
            "plan": "y" * 1000,
            "critic_notes": "z" * 1000,
            "top_posts_block": "p" * 1000,
            "regen_history": [{"feedback": "try again"}],
            "topic": "Old topic",
        }
    ]

    compacted = pipeline.compact_drafts_for_state(drafts)

    assert compacted[0]["sno"] == "old"
    assert compacted[0]["row_number"] == 9
    assert compacted[0]["thread_ts"] == "999.1"
    assert compacted[0]["topic"] == "Old topic"
    for key in ["draft", "plan", "critic_notes", "top_posts_block", "regen_history"]:
        assert key not in compacted[0]


def test_compacted_serialized_drafts_stay_below_safe_threshold():
    drafts = [
        {
            "sno": f"old-{i}",
            "status": "abandoned",
            "draft": "x" * 4000,
            "plan": "y" * 3000,
            "top_posts_block": "z" * 3000,
        }
        for i in range(20)
    ]

    compacted = pipeline.compact_drafts_for_state(drafts)

    assert len(json.dumps(compacted)) < pipeline.DRAFTS_STATE_MAX_CHARS


def test_maybe_generate_daily_draft_compacts_existing_state_before_save(monkeypatch):
    old_drafts = [
        {
            "sno": f"old-{i}",
            "status": "abandoned",
            "draft": "x" * 4000,
            "plan": "y" * 3000,
            "top_posts_block": "z" * 3000,
        }
        for i in range(10)
    ]
    state = FakeState(drafts=old_drafts)

    monkeypatch.setattr(pipeline, "should_force_daily_draft", lambda: True)
    monkeypatch.setattr(pipeline, "load_top_performing_posts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pipeline, "format_top_posts_for_prompt", lambda _posts: "")
    monkeypatch.setattr(
        pipeline,
        "generate_post",
        lambda *_args, **_kwargs: SimpleNamespace(
            draft="new draft text",
            plan="new plan",
            critic_verdict="PASS",
            critic_notes="",
            revision_count=0,
        ),
    )
    monkeypatch.setattr(pipeline, "post_draft", lambda *_args, **_kwargs: "111.222")

    pipeline.maybe_generate_daily_draft(
        state,
        slack=object(),
        anthropic_client=object(),
        channel_id="C123",
        registry=FakeRegistry(),
    )

    saved_drafts = state.saved["drafts"]
    assert saved_drafts[-1]["draft"] == "new draft text"
    assert saved_drafts[-1]["thread_ts"] == "111.222"
    assert "top_posts_block" not in saved_drafts[-1]
    assert len(json.dumps(saved_drafts)) < pipeline.DRAFTS_STATE_MAX_CHARS
