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

from config import ANTHROPIC_MODEL, CRITIC_MODEL, PROMPTS_DIR, get_logger

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
    """Topic pool for the AI-trend fallback. Defensively skips template prose,
    blockquotes, and any line that looks like placeholder/instructional text."""
    raw = _read_prompt_file("topics.md", fallback="")
    topics: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip headings, comments, blockquotes, horizontal rules
        if line.startswith(("#", "//", ">", "---", "===")):
            continue
        # Strip leading list markers like "- ", "* ", "1." (one pass — don't eat letters)
        for prefix in ("- ", "* ", "+ "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        else:
            # Numbered list "1. " etc.
            import re as _re
            m = _re.match(r"^\d+[.)]\s+", line)
            if m:
                line = line[m.end():].strip()
        if not line:
            continue
        # Skip placeholder lines that obviously aren't real topics
        if line.startswith("[") or line.startswith("("):
            continue
        if "[your" in line.lower() or "[add" in line.lower() or "[topic]" in line.lower():
            continue
        if "replace these" in line.lower() or "starter examples" in line.lower():
            continue
        if line.startswith("**") and line.endswith("**"):
            # bare bolded heading like **My topic pool**
            continue
        # Heuristic: a real topic should have at least 4 words and no placeholder vibes
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


@dataclass
class GenerationResult:
    """Bundle returned by generate_post(): the final text plus diagnostic fields
    so the pipeline can log/expose them in Slack."""
    draft: str
    plan: str
    critic_verdict: str           # "PASS" | "REVISE" | "FAIL"
    critic_notes: str             # human-readable feedback if not PASS
    revision_count: int = 0       # how many times we auto-revised


# --------------------------------------------------------------------------- #
# Prompt-caching helper                                                        #
# --------------------------------------------------------------------------- #


def _cached_system(text: str) -> list[dict]:
    """Wrap a system-prompt string into a content block with cache_control set.
    Anthropic charges 90% less for cached tokens. The system prompt is identical
    across every call so it benefits maximally from caching.
    Note: minimum cacheable size is 1024 tokens for Sonnet/Opus, 2048 for Haiku.
    Below the minimum, the API silently doesn't cache (no error)."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# --------------------------------------------------------------------------- #
# Step 1: Plan                                                                 #
# --------------------------------------------------------------------------- #

PLAN_SYSTEM = """You are an editor planning a LinkedIn post for Sagar before any drafting begins.

Output a structured plan with these sections, in this exact order, in plain text \
(NO markdown headers, NO bullets — just labeled blocks):

HOOK CONCEPT: A one-line description of the opening move. Specify the type \
(claim, story moment, specific number, sharp question, contrarian counter) and \
the actual content. Must be punchy enough to make readers click "see more" \
inside the first 210 characters of the post.

CENTRAL CLAIM: The single thesis the post defends, in 8-15 words.

NARRATIVE ARC: 3-5 numbered beats describing how the post moves from hook to \
close. Each beat is one sentence describing what that section does \
(e.g. "1. State the contrarian observation. 2. Concede the surface-level reason \
people believe the opposite. 3. Reveal the deeper mechanism most miss.").

EVIDENCE / EXAMPLES: 2-3 concrete things the post should reference — specific \
numbers, named patterns, real anecdotes. If you can't think of any, say so \
explicitly so the writer knows to invent specific-feeling detail rather than \
hide behind abstraction.

CLOSE: One sentence describing how the post ends — the closing thought, not \
just "wrap it up". Aim for a line readers might quote.

Do NOT write the post itself. Only the plan."""


def _plan_post(client: Anthropic, req: DraftRequest, about_me: str) -> str:
    user_prompt_parts = [f"TOPIC: {req.topic}"]
    if req.angle:
        user_prompt_parts.append(f"ANGLE (the writer's central claim, do not soften): {req.angle}")
    if req.key_points:
        user_prompt_parts.append(f"KEY POINTS to weave in:\n{req.key_points}")
    if req.voice:
        user_prompt_parts.append(f"VOICE: {req.voice}")
    if req.hook_style:
        user_prompt_parts.append(f"HOOK STYLE: {req.hook_style}")
    if req.cta:
        user_prompt_parts.append(f"CTA: {req.cta}")
    if about_me:
        user_prompt_parts.append("WHO THE WRITER IS:\n" + about_me)
    if req.feedback:
        user_prompt_parts.append(
            "USER FEEDBACK on the prior draft (the plan must directly address this):\n"
            + req.feedback
        )

    user_prompt = "\n\n".join(user_prompt_parts)
    LOG.info("Planning post (system=%d chars, user=%d chars)",
             len(PLAN_SYSTEM), len(user_prompt))
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_cached_system(PLAN_SYSTEM),
        messages=[{"role": "user", "content": user_prompt}],
    )
    plan = resp.content[0].text.strip()
    _log_cache_metrics("plan", resp)
    LOG.info("Plan generated (%d chars)", len(plan))
    return plan


def _log_cache_metrics(label: str, resp) -> None:
    """Surface cache hit/miss in logs so you can see prompt caching working."""
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
# Step 2: Write                                                                #
# --------------------------------------------------------------------------- #


def _write_post_from_plan(
    client: Anthropic, req: DraftRequest, plan: str,
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
        + "\n\n"
        "ROW METADATA:\n"
        + "\n".join(fields)
        + "\n\n"
        "Output ONLY the post text. No headers, no commentary, no markdown."
    )
    system = build_system_prompt()
    LOG.info("Writing post from plan (system=%d, user=%d)", len(system), len(user_prompt))
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        system=_cached_system(system),
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text.strip()
    _log_cache_metrics("write", resp)
    LOG.info("Draft from plan: %d chars, ~%d words", len(text), len(text.split()))
    return text


# --------------------------------------------------------------------------- #
# Step 3: Critique                                                             #
# --------------------------------------------------------------------------- #

CRITIC_SYSTEM = """You are a strict editor evaluating a LinkedIn post draft. The aim of \
the post is to attract readers and grow audience — generic-sounding posts fail.

Output ONLY these lines (no markdown, no extra commentary):

HOOK: <1-5>
CLARITY: <1-5>
EVIDENCE: <1-5>
DISTINCTIVENESS: <1-5>
LENGTH_FIT: <PASS|FAIL>
BANNED_PHRASES: <none | comma-separated list>
ENGAGEMENT: <LOW|MEDIUM|HIGH>
VERDICT: <PASS|REVISE|FAIL>
REVISION_NOTES: <if not PASS, 1-2 short sentences of concrete feedback. Empty otherwise.>

Verdict rules:
- PASS = ready to publish (be selective; default to REVISE if anything feels off)
- REVISE = workable but fixable issues (the auto-revision will use REVISION_NOTES)
- FAIL = fundamentally wrong (off-topic, misunderstood angle, meta-commentary about prompts)

Be strict and brief."""


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

    # Parse verdict + notes
    verdict = "PASS"
    notes_lines = []
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
# Top-level: generate_post() — plan, write, critique, maybe revise            #
# --------------------------------------------------------------------------- #


def generate_post(client: Anthropic, req: DraftRequest) -> GenerationResult:
    """The full pipeline: plan → write → critique → optional auto-revise.
    Returns a GenerationResult bundle for the caller to use."""
    about_me = load_about_me()

    plan = _plan_post(client, req, about_me)

    draft = _write_post_from_plan(client, req, plan)
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
        # For revisions, skip re-planning — just re-write against the original plan + critique
        try:
            new_draft = _write_post_from_plan(client, revision_req, plan)
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


def generate_draft(client: Anthropic, req: DraftRequest) -> str:
    """Backward-compatible thin wrapper. New code should use generate_post()."""
    return generate_post(client, req).draft


def pick_fallback_topic(used_recently: list[str]) -> str:
    """Pick a topic from prompts/topics.md that we haven't used in the last few days."""
    pool = load_topics()
    fresh = [t for t in pool if t not in used_recently]
    # Re-seed RNG so consecutive runs in the same minute pick differently
    random.seed(time.time())
    pick = random.choice(fresh) if fresh else random.choice(pool)
    LOG.info("Fallback topic picked: %r", pick[:80])
    return pick
