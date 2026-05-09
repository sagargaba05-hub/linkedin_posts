"""
generator.py — Anthropic-backed draft generator with plan→write→critique flow.

Loads prompt templates from prompts/ so Sagar can edit voice/about-me/topics
without touching code. Optionally accepts a list of top-performing past posts
to inject as few-shot examples (engagement feedback loop).
"""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

from config import ANTHROPIC_MODEL, CRITIC_MODEL, PROMPTS_DIR, get_logger
from reliability import anthropic_breaker, with_circuit, with_http_retries

LOG = get_logger("gen")


# --------------------------------------------------------------------------- #
# Prompt template loading                                                     #
# --------------------------------------------------------------------------- #


def _read_prompt_file(name: str, fallback: str = "") -> str:
    path = os.path.join(PROMPTS_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
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
    """Topic pool for the AI-trend fallback. Defensively skips template prose,
    blockquotes, and any line that looks like placeholder/instructional text."""
    raw = _read_prompt_file("topics.md", fallback="")
    topics: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("#", "//", ">", "---", "===")):
            continue
        for prefix in ("- ", "* ", "+ "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        else:
            m = re.match(r"^\d+[.)]\s+", line)
            if m:
                line = line[m.end():].strip()
        if not line:
            continue
        if line.startswith(("[", "(")):
            continue
        if "[your" in line.lower() or "[add" in line.lower() or "[topic]" in line.lower():
            continue
        if "replace these" in line.lower() or "starter examples" in line.lower():
            continue
        if line.startswith("**") and line.endswith("**"):
            continue
        if len(line.split()) < 3:
            continue
        topics.append(line)
    if not topics:
        topics = ["A thoughtful observation about how AI is changing knowledge work"]
    LOG.info("Loaded %d fallback topics", len(topics))
    for i, t in enumerate(topics[:5]):
        LOG.info("  topic[%d] = %r", i, t[:80])
    if len(topics) > 5:
        LOG.info("  ... and %d more", len(topics) - 5)
    return topics


# --------------------------------------------------------------------------- #
# System prompt assembly                                                       #
# --------------------------------------------------------------------------- #

# The base writing rules. Updated for short-form (150-200 words) with hashtags,
# whitespace, and scannable structure — optimized for LinkedIn engagement.
BASE_RULES = """You write LinkedIn posts in Sagar's voice. Output ONLY the post text — no preamble, no markdown headers, no commentary about the post.

LENGTH AND SHAPE (these are non-negotiable):
- Target 150-200 words. Stop when you've made the point — never pad.
- Short paragraphs: 1-2 sentences each. Maximum 3.
- Blank line between every paragraph. White space is a feature, not waste.
- Use bullet points or numbered lists when listing distinct ideas. Bullets should be tight (5-12 words each); never write paragraphs as bullets.
- One idea per line where it improves scannability — fragments are fine on LinkedIn.

HOOK (line 1 only, ~210 characters max — that's the LinkedIn truncation point):
- Open with a claim, a specific number, a sharp question, or a contrarian counter.
- Never open with "I am excited to" / "Thrilled to share" / "In today's fast-paced world" / "Reflecting on my journey".
- The first line decides whether anyone reads the rest. Make it earn the click.

CORE STRUCTURE:
1. Hook (line 1, ~10-15 words)
2. Brief setup or claim (1 short paragraph)
3. The substance — could be 2-4 short paragraphs OR a tight bulleted list OR alternating short paragraphs and bullets
4. A close that lands — one line, quotable if possible

HASHTAGS:
- Add 2-4 relevant hashtags at the end of the post, on their own line, separated by single spaces.
- Use specific tags (#productmanagement, #aigovernance) over generic ones (#leadership, #motivation).
- Never inline hashtags inside paragraphs.
- Skip hashtags entirely only if the post is intentionally personal/raw and tags would feel off-brand.

WHAT NOT TO DO:
- No AI cliches: "game-changer", "leverage", "deep dive", "synergy", "unlock the power of", "future of", "revolutionize", "moving the needle", "let's dive in".
- No motivational fluff. No "remember,". No "And that's the takeaway".
- No engagement-bait questions ("Agree?" / "What do you think?") unless you have a real reason.
- No emojis unless explicitly told to add one.
- Never quote yourself or invent fake testimonials.

VOICE MATCHING:
- thoughtful: considered, slightly contrarian, evidence over enthusiasm.
- conversational: story-led with a personal moment, lighter tone, can ask one real question.
- punchy: strong opening claim, very short paragraphs, designed to provoke discussion.

HOOK STYLES:
- question: open with a sharp specific question (not rhetorical fluff).
- story: open mid-scene with a concrete moment ("Last Tuesday at 11:47 PM, I...").
- contrarian: open with the opposite of conventional wisdom.
- stat: open with a specific number that stops the scroll.

CTA / CLOSE:
- If the row provides a CTA, that's the closer.
- Otherwise close with a sharp thought, not a generic question.
- If a Link is provided, weave it into a relevant sentence near the end — never as a standalone footer."""


def build_system_prompt(top_posts_block: str = "") -> str:
    """Assemble: base rules + about_me + voice examples + (optional) top posts."""
    parts = [BASE_RULES]

    about = load_about_me()
    if about:
        parts.append("ABOUT THE WRITER:\n" + about)

    voice_ex = load_voice_examples()
    if voice_ex:
        parts.append("VOICE REFERENCE — emulate this style and avoid the listed traps:\n" + voice_ex)

    if top_posts_block:
        parts.append(top_posts_block)

    return "\n\n---\n\n".join(parts)


def _cached_system(text: str) -> list[dict]:
    """Wrap a system-prompt string in cache_control so Anthropic charges 90% less
    on cached tokens. Identical across calls -> max cache hit rate."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _log_cache_metrics(label: str, resp) -> None:
    try:
        u = resp.usage
        LOG.info(
            "[cache] %s: input=%s, cache_create=%s, cache_read=%s, output=%s",
            label,
            getattr(u, "input_tokens", "?"),
            getattr(u, "cache_creation_input_tokens", "?"),
            getattr(u, "cache_read_input_tokens", "?"),
            getattr(u, "output_tokens", "?"),
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Data classes                                                                #
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
    feedback: str = ""


@dataclass
class GenerationResult:
    draft: str
    plan: str
    critic_verdict: str
    critic_notes: str
    revision_count: int = 0


# --------------------------------------------------------------------------- #
# Plan step                                                                    #
# --------------------------------------------------------------------------- #

PLAN_SYSTEM = """You are an editor planning a 150-200 word LinkedIn post for Sagar before any drafting begins.

Output a structured plan with these sections, in this exact order, in plain text:

HOOK CONCEPT: One line describing the opening move (claim / story moment / specific number / sharp question / contrarian counter) and the actual content. Max ~15 words. Must work in <210 chars before the LinkedIn truncation cutoff.

CENTRAL CLAIM: The single thesis the post defends, in 8-15 words.

STRUCTURE: One of:
 - "narrative" (3 short paragraphs flowing into each other)
 - "list" (hook + 1-line setup + 3-5 bullets + 1-line close)
 - "hybrid" (hook + 1 paragraph + 3 bullets + 1 closing paragraph)
Pick what fits the content best — bullets are great for distinct ideas, narrative for an arc.

KEY POINTS (bullet form, 3-5 items): the actual content the post should make. Keep each point to 8-15 words.

CLOSE: One sentence describing how the post ends — the closing thought. Aim for a quotable line.

HASHTAGS: 2-4 specific hashtags this post should end with.

Do NOT write the post itself. Only the plan."""


@with_circuit(anthropic_breaker)
@with_http_retries
def _plan_post(client: Anthropic, req: DraftRequest, about_me: str) -> str:
    parts = [f"TOPIC: {req.topic}"]
    if req.angle:
        parts.append(f"ANGLE (the central claim, do not soften): {req.angle}")
    if req.key_points:
        parts.append(f"KEY POINTS to weave in:\n{req.key_points}")
    if req.voice:
        parts.append(f"VOICE: {req.voice}")
    if req.hook_style:
        parts.append(f"HOOK STYLE: {req.hook_style}")
    if req.cta:
        parts.append(f"CTA: {req.cta}")
    if about_me:
        parts.append("WHO THE WRITER IS:\n" + about_me)
    if req.feedback:
        parts.append("USER FEEDBACK on the prior draft (the plan must address this):\n" + req.feedback)

    user_prompt = "\n\n".join(parts)
    LOG.info("Planning post (system=%d, user=%d)", len(PLAN_SYSTEM), len(user_prompt))
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=768,
        system=_cached_system(PLAN_SYSTEM),
        messages=[{"role": "user", "content": user_prompt}],
    )
    plan = resp.content[0].text.strip()
    _log_cache_metrics("plan", resp)
    LOG.info("Plan generated (%d chars)", len(plan))
    return plan


# --------------------------------------------------------------------------- #
# Write step                                                                  #
# --------------------------------------------------------------------------- #


@with_circuit(anthropic_breaker)
@with_http_retries
def _write_post_from_plan(
    client: Anthropic, req: DraftRequest, plan: str, top_posts_block: str = "",
) -> str:
    fields = [f"Topic: {req.topic}"]
    if req.angle:
        fields.append(f"Angle: {req.angle}")
    if req.key_points:
        fields.append(f"Key points: {req.key_points}")
    if req.voice:
        fields.append(f"Voice: {req.voice}")
    if req.hook_style:
        fields.append(f"Hook style: {req.hook_style}")
    if req.link:
        fields.append(f"Link: {req.link}")
    if req.cta:
        fields.append(f"CTA: {req.cta}")
    if req.feedback:
        fields.append(f"User feedback to address: {req.feedback}")

    user_prompt = (
        "Write the LinkedIn post following this plan exactly. The plan was prepared "
        "by an editor — your job is to execute it well, not second-guess it.\n\n"
        "PLAN:\n"
        + plan
        + "\n\nROW METADATA:\n"
        + "\n".join(fields)
        + "\n\nOutput ONLY the post text. No headers, no commentary, no markdown."
    )
    system = build_system_prompt(top_posts_block)
    LOG.info("Writing post from plan (system=%d, user=%d)", len(system), len(user_prompt))
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_cached_system(system),
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text.strip()
    _log_cache_metrics("write", resp)
    LOG.info("Draft from plan: %d chars, ~%d words", len(text), len(text.split()))
    return text


# --------------------------------------------------------------------------- #
# Critique step                                                                #
# --------------------------------------------------------------------------- #

CRITIC_SYSTEM = """You are a strict editor evaluating a LinkedIn post draft. The aim is to attract readers and grow audience.

Output ONLY these lines (no markdown, no extra commentary):

HOOK: <1-5>
CLARITY: <1-5>
SCANNABILITY: <1-5>
DISTINCTIVENESS: <1-5>
LENGTH_FIT: <PASS|FAIL>  (must be 130-220 words; outside that range -> FAIL)
HASHTAGS: <PASS|FAIL>  (must have 2-4 hashtags on a final line; otherwise FAIL)
BANNED_PHRASES: <none | comma-separated list>
ENGAGEMENT: <LOW|MEDIUM|HIGH>
VERDICT: <PASS|REVISE|FAIL>
REVISION_NOTES: <if not PASS, 1-2 short concrete sentences. Empty otherwise.>

Verdict rules:
- PASS = ready to publish (be selective; default to REVISE if anything feels off)
- REVISE = workable but fixable issues (auto-revision will use REVISION_NOTES)
- FAIL = fundamentally wrong (off-topic, misunderstood angle, meta-commentary about prompts)

Be strict and brief."""


@with_circuit(anthropic_breaker)
@with_http_retries
def _critique_post(client: Anthropic, draft: str, req: DraftRequest) -> dict:
    user_prompt = (
        f"Topic: {req.topic}\n"
        f"Angle: {req.angle or '(not provided)'}\n"
        f"Voice: {req.voice or '(not specified)'}\n\n"
        f"DRAFT TO EVALUATE:\n---\n{draft}\n---"
    )
    LOG.info("Running critic (model=%s)", CRITIC_MODEL)
    resp = client.messages.create(
        model=CRITIC_MODEL,
        max_tokens=512,
        system=_cached_system(CRITIC_SYSTEM),
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text.strip()
    _log_cache_metrics("critique", resp)
    LOG.info("Critic output (%d chars):\n%s", len(text), text[:600])

    return parse_critic_output(text)


def parse_critic_output(text: str) -> dict:
    """Pure-logic parser for critic output. Extracted so it's testable."""
    verdict = "PASS"
    notes_lines: list[str] = []
    in_notes = False
    for line in text.splitlines():
        upper = line.upper().strip()
        if upper.startswith("VERDICT:"):
            after = line.split(":", 1)[1].strip()
            if "FAIL" in after.upper():
                verdict = "FAIL"
            elif "REVISE" in after.upper():
                verdict = "REVISE"
            else:
                verdict = "PASS"
        elif upper.startswith("REVISION_NOTES:"):
            in_notes = True
            after = line.split(":", 1)[1].strip()
            if after:
                notes_lines.append(after)
        elif in_notes and line.strip():
            notes_lines.append(line.strip())
    notes = " ".join(notes_lines).strip()
    return {"verdict": verdict, "notes": notes, "raw": text}


# --------------------------------------------------------------------------- #
# Top-level pipeline                                                          #
# --------------------------------------------------------------------------- #


def generate_post(
    client: Anthropic,
    req: DraftRequest,
    top_posts_block: str = "",
) -> GenerationResult:
    """plan → write → critique → optional auto-revise. Returns a GenerationResult."""
    about_me = load_about_me()
    plan = _plan_post(client, req, about_me)
    draft = _write_post_from_plan(client, req, plan, top_posts_block)
    crit = _critique_post(client, draft, req)
    revisions = 0

    if crit["verdict"] in ("REVISE", "FAIL"):
        LOG.info("Critic returned %s — auto-revising once", crit["verdict"])
        revision_req = DraftRequest(
            topic=req.topic, angle=req.angle, key_points=req.key_points,
            voice=req.voice, hook_style=req.hook_style, link=req.link, cta=req.cta,
            feedback=("EDITOR FEEDBACK on previous draft (address every point): "
                      + crit["notes"]),
        )
        try:
            new_draft = _write_post_from_plan(client, revision_req, plan, top_posts_block)
            new_crit = _critique_post(client, new_draft, req)
            draft = new_draft
            crit = new_crit
            revisions = 1
        except Exception:
            LOG.exception("Auto-revision failed; using original draft + critic notes")

    return GenerationResult(
        draft=draft,
        plan=plan,
        critic_verdict=crit["verdict"],
        critic_notes=crit["notes"],
        revision_count=revisions,
    )


def pick_fallback_topic(used_recently: list[str]) -> str:
    pool = load_topics()
    fresh = [t for t in pool if t not in used_recently]
    random.seed(time.time())
    pick = random.choice(fresh) if fresh else random.choice(pool)
    LOG.info("Fallback topic picked: %r", pick[:80])
    return pick
