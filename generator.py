"""
generator.py — Anthropic-backed draft generator. Loads prompt templates from
the prompts/ folder so Sagar can edit voice/about-me/topics without touching code.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass

from anthropic import Anthropic

from config import ANTHROPIC_MODEL, PROMPTS_DIR, get_logger

LOG = get_logger("gen")


# --------------------------------------------------------------------------- #
# Prompt template loading                                                     #
# --------------------------------------------------------------------------- #


def _read_prompt_file(name: str, fallback: str = "") -> str:
    """Load a prompt template from prompts/<name>. Missing files are tolerated
    (returns the fallback) so the script doesn't crash before Sagar fills them in."""
    path = os.path.join(PROMPTS_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                LOG.info("Loaded prompt: %s (%d chars)", name, len(content))
                return content
    except FileNotFoundError:
        LOG.warning("Prompt file not found: %s — using fallback", name)
    return fallback


def load_about_me() -> str:
    return _read_prompt_file(
        "about_me.md",
        fallback="Sagar — a professional sharing LinkedIn posts on topics he cares about.",
    )


def load_voice_examples() -> str:
    return _read_prompt_file("voice_examples.md", fallback="")


def load_topics() -> list[str]:
    """Topic pool for the AI-trend fallback. Each non-blank line is one topic."""
    raw = _read_prompt_file("topics.md", fallback="")
    topics: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        # Skip blanks, headings, and obvious commentary
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # Strip leading list markers like "- " or "* " or "1."
        line = line.lstrip("-*0123456789. ").strip()
        if line:
            topics.append(line)
    if not topics:
        topics = ["A thoughtful observation about how AI is changing knowledge work"]
    LOG.info("Loaded %d fallback topics", len(topics))
    return topics


# --------------------------------------------------------------------------- #
# System prompt assembly                                                       #
# --------------------------------------------------------------------------- #

BASE_RULES = """You write LinkedIn posts in Sagar's voice. Output ONLY the post text — no preamble, no markdown headers, no commentary.

WRITING PRINCIPLES (these are non-negotiable):
- LENGTH. Target ~500 words (range 450-550). This is a long-form thought-leadership format — use the room to develop one idea with real substance, examples, and nuance. Length is not a license for filler; every paragraph still earns its place.
- HOOK in line 1. LinkedIn truncates after ~210 characters; the opening must make readers click "see more". A clear claim, a specific number, a story moment, or a sharp question. Never open with "I am excited to" / "Thrilled to share" / "In today's fast-paced world".
- ONE central idea per post. If you can't summarize the post in 8 words, it's too unfocused. The 500 words develop and defend ONE thesis — they don't cover three loosely-related thoughts.
- PLAIN sentences. Short paragraphs (1-3 sentences each), with blank-line spacing between them. Long posts MUST be scannable — readers skim before they commit.
- HONEST. If the row provides an Angle, that is the post's central claim — do not water it down to be safer.
- NO EMOJIS unless explicitly told to add one.
- NO HASHTAG STACKS. Maximum 2 hashtags, only if they're actually relevant. None is usually better.
- NO AI CLICHES: "game-changer", "unlock the power of", "let's dive in", "the future of", "revolutionize", "unleash", "leverage", "synergy", "deep dive", "moving the needle".
- NO MOTIVATIONAL FLUFF. No "remember", no "and that's the takeaway".

VOICE MATCHING:
- If Voice = thoughtful: considered, slightly contrarian, evidence over enthusiasm.
- If Voice = conversational: story-led with a personal moment, lighter tone, can ask one question at the end.
- If Voice = punchy: strong opening claim, very short paragraphs, designed to provoke discussion.

HOOK STYLES:
- question: open with a sharp question (not rhetorical fluff — a real question).
- story: open mid-scene with a concrete moment ("Last Tuesday at 11:47 PM, I...").
- contrarian: open with the opposite of conventional wisdom.
- stat: open with a specific number that stops the scroll.

CLOSING:
- If the row provides a CTA, use it as the closer.
- Otherwise close with a thought, not a generic question, unless Voice=conversational.
- If a Link is provided, weave it naturally into a relevant sentence near the end — never as a standalone footer."""


def build_system_prompt() -> str:
    """Assemble the full system prompt from base rules + about_me + voice examples."""
    parts = [BASE_RULES]

    about = load_about_me()
    if about:
        parts.append("ABOUT THE WRITER:\n" + about)

    voice_ex = load_voice_examples()
    if voice_ex:
        parts.append("VOICE REFERENCE — emulate this style and avoid the listed traps:\n" + voice_ex)

    return "\n\n---\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Generation                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class DraftRequest:
    topic: str
    angle: str = ""
    key_points: str = ""
    voice: str = ""
    hook_style: str = ""
    link: str = ""
    cta: str = ""
    feedback: str = ""  # for regenerations


def generate_draft(client: Anthropic, req: DraftRequest) -> str:
    """Call Claude to produce the LinkedIn post text."""
    fields = [f"Topic: {req.topic}"]
    if req.angle:
        fields.append(f"Angle (the central claim — do not soften): {req.angle}")
    if req.key_points:
        fields.append(f"Key points to weave in:\n{req.key_points}")
    if req.voice:
        fields.append(f"Voice: {req.voice}")
    if req.hook_style:
        fields.append(f"Hook style: {req.hook_style}")
    if req.link:
        fields.append(f"Link to include: {req.link}")
    if req.cta:
        fields.append(f"CTA: {req.cta}")
    if req.feedback:
        fields.append(
            f"\nUSER FEEDBACK on the previous draft (address this directly, "
            f"don't just slightly tweak): {req.feedback}"
        )

    user_prompt = "Write a LinkedIn post for this:\n\n" + "\n".join(fields)
    system = build_system_prompt()
    LOG.info("Generating draft (model=%s system_chars=%d user_chars=%d)",
             ANTHROPIC_MODEL, len(system), len(user_prompt))

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text.strip()
    LOG.info("Draft generated (%d chars, ~%d words)", len(text), len(text.split()))
    return text


def pick_fallback_topic(used_recently: list[str]) -> str:
    """Pick a topic from prompts/topics.md that we haven't used in the last few days.
    Falls back to a random pick if everything's been used recently."""
    pool = load_topics()
    fresh = [t for t in pool if t not in used_recently]
    pick = random.choice(fresh) if fresh else random.choice(pool)
    # Shuffle the seed slightly with current time so consecutive runs vary
    random.seed(time.time())
    LOG.info("Fallback topic picked: %r", pick[:80])
    return pick
