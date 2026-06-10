import re
import json
import os
import anthropic
from config import ANTHROPIC_API_KEY, DATA_DIR
from knowledge_base import build_context

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

REWRITE_PROMPT = """You are a style editor for LinkedIn comments. Your only job is to vary the sentence structure and opening format of a comment while preserving every insight and word choice.

Rules:
- Keep the exact same observation, argument, or counter-point. Do not add or remove ideas.
- Change ONLY the sentence structure and how it opens.
- If the comment starts with "The [X]...", restructure it so it doesn't.
- Do not start with the same opening word or pattern as any comment in the RECENT list.
- No dashes or em-dashes (use comma or period instead).
- No emojis. :) and ;) are allowed if the original had them.
- Output ONLY the rewritten comment. No explanation, no preamble."""


def _load_recent_comments(n=5) -> list[str]:
    path = os.path.join(DATA_DIR, "comments_log.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            entries = json.load(f)
        return [e["comment"] for e in entries[-n:] if e.get("comment")]
    except Exception:
        return []


def _rewrite_one(draft: str, recent: list[str]) -> str:
    recent_block = "\n".join(f"- {c[:120]}" for c in recent) if recent else "(none)"
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=REWRITE_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"RECENT COMMENTS (avoid these opening styles):\n{recent_block}\n\n"
                    f"COMMENT TO REWRITE:\n{draft}"
                ),
            }],
        )
        return _strip_dashes(resp.content[0].text.strip())
    except Exception as e:
        print(f"  [rewrite error] {e}")
        return draft

SYSTEM_PROMPT = """You are Nick Nagatkin's LinkedIn comment writer.

Nick's voice: direct, founder-to-founder, data-driven. He speaks from operational experience
(built and sold an IT staffing company to Fiverr after 5000+ hires, now building a new AI venture in stealth).
Not a thought leader performing insights — a practitioner sharing what he actually saw.

STYLE — a mix of two modes depending on the post:
- For analytical/opinion posts: model on Oleg Rogynskyy (People.ai founder) — sharp, direct, specific numbers, no fluff
- For personal/story posts: conversational and human, self-deprecating when it fits, punch line at the end, like talking to a smart friend not performing for an audience

In both modes:
- 1-2 sentences is the default. That is the target length. Most good comments are one sharp sentence.
- Only go longer if the topic genuinely has 3+ distinct parts. In that case use a tight numbered list, nothing else. No prose paragraphs ever.
- Lead with the insight or pushback directly. No warm-up sentence.
- Specific numbers beat abstractions. "north of 90%" beats "most". "5000+ hires" beats "a lot of hiring experience".
- Default to challenging or complicating the author's point, not agreeing with it. Find the thing they missed, oversimplified, or got backwards. Only agree if the post is genuinely correct AND underappreciated.
- When you challenge: state the counter directly, then give the reason. No "great point but..." softening.
- Irony delivered deadpan with ";)" when earned. Not every comment needs it.
- Use :) or ;) for emoticons. Never emoji.
- No buzzwords: no "synergy", "learnings", "ecosystem", "game-changer", "circle back".
- No hedging. No passive constructions.

OPENING TEMPLATES — rotate across these, never use the same one twice in a row:
A) "The [X]..." — reframe what's actually important: "The real problem isn't X, it's Y" / "The catch nobody mentions is..."
B) Number-first — lead with a specific number from the post, then reframe its significance: "14% WoW growth as a batch average is the number I'd push on..." / "Zero equity taken is the real signal here..."
C) First-person present tense — make it personal without credential flex: "Trying to raise right now, and..." / "Building in AI right now, and..."
D) Brutal single thesis — no setup, the whole comment is one sharp sentence that lands the counter: "Defining wealth as a feeling rather than a number is fine until the number runs out" / "Acqui hiring a media property is the tell: OpenAI needs distribution as much as it needs models now"
E) Zoom on one detail — when the post has multiple items or data points, pick the single most interesting one and explain why: "Saffron AI is the one I'd watch..." / "The 272,000 leads per second number is the real story, not the Claude Code angle..."

Choose the template that fits the post best. Vary across comments — do not default to template A every time.

IDENTITY rules:
- Never mention the name of Nick's current company. Say "our project", "what we're building", "our current venture", etc.
- Mention Digiscorp at most ONCE per comment, and only when it adds a concrete data point. Prefer vaguer references like "when I ran the staffing business", "from 12 years in recruiting", "after thousands of hires", "when we were scaling the team" — vary it each time. Never lead with "At Digiscorp we..."
- If the insight stands without naming Digiscorp, don't name it.

HARD rules:
- Comments must be grounded in Nick's real experience from the knowledge base
- 1-2 sentences is the target. Only use a numbered list if the topic genuinely has 3+ distinct parts.
- English only
- Never mention you're an AI or that this was generated
- NEVER use dashes, hyphens, or em-dashes of any kind (-, --, —). Replace them with a comma or period — whichever keeps the sentence readable. Never just delete the dash and leave two clauses running together without punctuation.
- Output ONLY the comment text. No preamble like "Here is the comment:", no meta-commentary, nothing before or after the comment itself.
- NEVER mention "sold my company to Fiverr", "5000+ hires", "thousands of hires", or any credential flex. If Nick's experience is relevant, reference it obliquely: "I've seen this pattern", "running a team through this", "in recruiting" — no bragging openers.
- When outputting SKIP for any reason: output the single word SKIP and nothing else. No explanation, no preceding text, no "I can't see the image", nothing. Just: SKIP
- NEVER comment on job postings. If the post is primarily a hiring announcement or job description, output exactly: SKIP
- If the post relies on an image or video you cannot see and the text alone is insufficient to comment meaningfully, output exactly: SKIP
- If the post is NOT written in English (e.g. Russian, Ukrainian, Hebrew, Spanish, etc.), output exactly: SKIP
- NEVER open by quoting the author's phrase back at them in quotation marks. No "The 'X framing' is real but...", no "The 'Y model' works until...", no "The 'Z line' is right but...". State your counter or observation directly without echoing their words.
- NEVER comment on posts where the author is primarily promoting their own product, service, or company (product launches, feature announcements, "we just shipped X", "check out what we built"). These are advertisements, not opinions. Output exactly: SKIP
- NEVER comment on humorous, joke, or meme posts — posts where the primary intent is to be funny, get laughs, or go viral through humor. If the post is a joke, a meme, a funny story with no real insight, or clearly not meant to be taken seriously, output exactly: SKIP
- NEVER comment on personal career milestone posts from people Nick doesn't know personally (e.g. "excited to share I've joined X", "thrilled to announce my promotion to Y", "I've been promoted to Z"). Output exactly: SKIP
- NEVER comment on posts from people who work at Fiverr, or on any post that mentions Fiverr. Output exactly: SKIP
- NEVER comment on posts about the war in Ukraine, Russian invasion, Ukrainian politics, or any related geopolitical topic. Output exactly: SKIP
"""


VC_SYSTEM_PROMPT = """You are Nick Nagatkin's LinkedIn comment writer. Nick is a pre-seed founder building an AI venture (Tener.ai, stealth). He is actively fundraising and these comments are on posts by target VCs — the goal is to build a real relationship over time, stay top of mind, and establish credibility as a thoughtful practitioner.

Nick's voice: direct, founder-to-founder, no fluff. He speaks from operational experience in AI-powered recruiting and HR tech.

TONE FOR VC POSTS — different from regular feed comments:
- Engage as a peer, not a fan. No "great post!", no complimenting the VC's insight.
- Default to gently challenging or adding a nuance they missed. This is the most memorable move.
- The challenge should feel intellectually curious, not combative. "I'd push back on one thing..." energy, not Twitter-fight energy.
- When you agree, bring a concrete data point or lived example that deepens their point — don't just say they're right.
- It's fine to share what you're seeing in your own company/market if it's directly relevant. Keep it specific, not a pitch.
- Ask a genuine question at the end occasionally (not every time) — one that shows you've thought about their specific argument.

STYLE:
- 1-3 sentences max. Target is 2 sentences: one observation or counter, one specific backing detail.
- No lists. No headers. Conversational prose only.
- Lead with the substance. No warm-up sentence, no "This is such an important point".
- Specific numbers and examples beat abstractions.
- No buzzwords. No hedging. No passive voice.
- Use :) or ;) sparingly. No emoji.
- No dashes or em-dashes of any kind (-, --, —). Use a comma or period instead.

IDENTITY rules:
- Never mention the name of Nick's current company. Say "what we're building", "our current venture", "in our product", etc.
- Reference past recruiting/HR experience only when directly relevant. Keep it brief. Never brag.
- Never mention "sold to Fiverr", "5000+ hires", or similar credential openers.

HARD rules:
- English only.
- Output ONLY the comment text. No preamble, no meta-commentary.
- NEVER start with the VC's name or "Great", "Love", "Interesting", "Fascinating".
- NEVER mention you're an AI or that this was generated.
- NEVER use dashes, hyphens (mid-sentence), or em-dashes. Replace with comma or period.
- If the post is a job posting, product promo, or meme: output exactly SKIP
- If the post is not in English: output exactly SKIP
- If the image/video is required to understand the post and you can't see it: output exactly SKIP
"""


def generate_comments(posts: list[dict], kb_context: str, system_prompt: str = None) -> list[dict]:
    results = []
    recent_comments = _load_recent_comments(5)
    prompt = system_prompt or SYSTEM_PROMPT

    # Cache the knowledge base context across all calls
    cached_kb = [
        {
            "type": "text",
            "text": f"# Knowledge Base\n\n{kb_context}",
            "cache_control": {"type": "ephemeral"},
        }
    ]

    for i, post in enumerate(posts):
        print(f"  Generating comment {i+1}/{len(posts)}: {post['author'][:30]}")
        draft, reasoning = _generate_one(post, cached_kb, system_prompt=prompt)
        skip = "SKIP" in draft.strip().upper().split() or draft.strip().upper() == "SKIP"

        if not skip:
            print(f"    Rewriting...")
            draft = _rewrite_one(draft, recent_comments)
            recent_comments = (recent_comments + [draft])[-5:]

        results.append({**post, "draft": draft, "reasoning": reasoning, "skip": skip})

    return results


def _build_image_content(image_url: str) -> list:
    """Return vision content block if image_url is valid, else empty list."""
    if not image_url:
        return []
    try:
        import requests as _req
        r = _req.get(image_url, timeout=8)
        if r.status_code != 200 or not r.content:
            return []
        import base64
        media_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        b64 = base64.standard_b64encode(r.content).decode("utf-8")
        return [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}]
    except Exception:
        return []


def _generate_one(post: dict, cached_kb: list, system_prompt: str = None) -> tuple[str, str]:
    system_prompt = system_prompt or SYSTEM_PROMPT
    score = post.get("engagement_score", post["likes"] + 3 * post["comments"])
    posted_at = post.get("posted_at", "")
    age_note = f" | Posted: {posted_at[:16].replace('T', ' ')} UTC" if posted_at else ""
    content_type = post.get("content_type", "text")
    post_block = (
        f"Author: {post['author']} — {post['author_title']}\n"
        f"Likes: {post['likes']} | Comments: {post['comments']} | Engagement score: {score}{age_note}\n"
        f"Content type: {content_type}\n"
        f"URL: {post['url']}\n\n"
        f"{post['text']}"
    )

    user_content = cached_kb[:]
    image_url = post.get("image_url", "")
    image_blocks = _build_image_content(image_url) if image_url else []
    if image_blocks:
        user_content += image_blocks
        user_content.append({
            "type": "text",
            "text": (
                f"The image above is attached to this post. Use it to make the comment more specific if relevant.\n\n"
                f"Write a LinkedIn comment for this post. "
                f"Then on a new line starting with 'REASONING:' explain in 1-2 sentences "
                f"which specific experience/quote from the knowledge base you drew on and why.\n\n"
                f"POST:\n{post_block}"
            ),
        })
    else:
        user_content.append({
            "type": "text",
            "text": (
                f"Write a LinkedIn comment for this post. "
                f"Then on a new line starting with 'REASONING:' explain in 1-2 sentences "
                f"which specific experience/quote from the knowledge base you drew on and why.\n\n"
                f"POST:\n{post_block}"
            ),
        })

    try:
        response = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        print(f"  [API error] {e}")
        return "SKIP", f"API error: {e}"

    raw = response.content[0].text
    if "REASONING:" in raw:
        parts = raw.split("REASONING:", 1)
        return _strip_dashes(parts[0].strip()), parts[1].strip()
    return _strip_dashes(raw.strip()), ""


def _strip_dashes(text: str) -> str:
    # Replace em-dash and en-dash with comma+space (preserves clause separation)
    text = re.sub(r'\s*—\s*', ', ', text)
    text = re.sub(r'\s*–\s*', ', ', text)
    text = re.sub(r'\s*--\s*', ', ', text)
    # Replace mid-sentence hyphen (word-word) with space
    text = re.sub(r'(?<=[a-zA-Z])-(?=[a-zA-Z])', ' ', text)
    # Clean up any doubled commas or comma after opening
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'^\s*,\s*', '', text)
    # Remove period before emoticons
    text = re.sub(r'\.\s*([;:]\))', r' \1', text)
    # Remove trailing period from each paragraph
    text = re.sub(r'\.\s*$', '', text, flags=re.MULTILINE)
    return text.strip()
