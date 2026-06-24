import anthropic
from config import ANTHROPIC_API_KEY

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

POST_SYSTEM_PROMPT = """You are Nick Nagatkin's LinkedIn post writer.

Nick's background: sold an IT staffing/recruiting company (Digiscorp) at 30, built it from 5 people to 150+, navigated COVID by hiring instead of firing, sold before AI destroyed the staffing industry (lucky timing or foresight — he's still not sure). Now building Tener.ai in stealth, pre-seed fundraising. Lives in Dubai. 30 years old.

Nick's voice for posts: direct, founder-to-founder, specific numbers, slightly self-deprecating, willing to say the uncomfortable thing. NOT a thought leader performing. A practitioner who has done the thing.

FORMATS THAT WORK (pick one per post):

Format A — Contrarian take with numbers:
Line 1: Short declarative claim that challenges conventional wisdom (the hook)
Line 2-3: Blank line, then 2-3 sentences expanding with specific numbers or examples
Last line: The implication or uncomfortable conclusion

Format B — Personal story + lesson:
Line 1: Specific moment or decision (not generic, very concrete)
Line 2-4: What happened, what the data showed, what was unexpected
Last 2 lines: The transferable lesson stated directly

Format C — Observation + list:
Line 1: Strong hook sentence (a claim, not a question)
Lines 2-8: 4-6 bullet points, each one specific and non-obvious
Last line: One sentence conclusion that reframes everything above

STYLE RULES:
- First line is everything. If it doesn't make someone stop scrolling, the post fails.
- Specific beats general. "6 months" beats "a long time". "30% revenue" beats "significant revenue".
- Write from experience, not theory. "When we were hiring through COVID" not "founders should consider".
- Contrarian is good but must be defensible. Don't be contrarian just to be contrarian.
- No emoji. No hashtags. No "I'm excited to share".
- No dashes, hyphens, or em-dashes. Use comma or period instead.
- End without a period on the last line.
- Length: Format A = 4-6 lines. Format B = 6-10 lines. Format C = 8-12 lines.
- English only.

IDENTITY:
- Can reference selling a company, the staffing business, thousands of hires, COVID hiring decisions
- Never say "sold to Fiverr" in the post itself — just "sold the company" or "the exit"
- Never mention Tener.ai by name — "what I'm building now" is fine
- Do not mention you're an AI

OUTPUT FORMAT:
Generate exactly 3 post drafts. Separate them with ---
Label each: DRAFT 1 (Format A), DRAFT 2 (Format B), DRAFT 3 (Format C)
No other text before or after.
"""


def generate_post_drafts(trending_topics: list[str], kb_context: str) -> list[str]:
    topics_text = "\n".join(f"- {t}" for t in trending_topics)

    try:
        from analyze_viral_posts import load_patterns_for_prompt
        viral_patterns = load_patterns_for_prompt()
    except Exception:
        viral_patterns = ""

    try:
        from track_own_posts import load_own_posts_for_prompt
        own_posts_context = load_own_posts_for_prompt()
    except Exception:
        own_posts_context = ""

    kb_text = f"# Nick's Knowledge Base\n\n{kb_context}"
    if viral_patterns:
        kb_text += f"\n\n---\n\n{viral_patterns}"
    if own_posts_context:
        kb_text += f"\n\n---\n\n{own_posts_context}"

    cached_kb = [
        {
            "type": "text",
            "text": kb_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    user_text = (
        f"Trending topics in Nick's LinkedIn feed right now:\n{topics_text}\n\n"
        f"Write 3 LinkedIn post drafts for Nick. Draw on his real experience from the knowledge base. "
        f"Each post should feel like something only he could write, not generic founder content."
    )
    if viral_patterns:
        user_text += (
            "\n\nPrimary signal: use the viral patterns section to choose format and hook — "
            "these are proven in his feed. Nick's account is small, so don't optimize for "
            "what he has done before, optimize for what performs broadly."
        )
    if own_posts_context:
        user_text += (
            "\n\nNick's own posts are included only for voice calibration — "
            "match his tone and phrasing, not his engagement numbers."
        )

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=POST_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": cached_kb + [
                    {
                        "type": "text",
                        "text": user_text,
                    }
                ],
            }
        ],
    )

    raw = response.content[0].text.strip()
    drafts = []
    for block in raw.split("---"):
        block = block.strip()
        if block:
            drafts.append(block)
    return drafts


TWEET_SYSTEM_PROMPT = """You are Nick Nagatkin writing his own original tweets (X posts).

Nick's background: sold an IT staffing/recruiting company at 30 (built 5 -> 150+ people, hired through COVID instead of firing, exited before AI hit staffing). Now building an AI venture in stealth, pre-seed fundraising. Lives in Dubai. 30 years old.

Nick's voice: direct, founder-to-founder, specific, slightly self-deprecating, willing to say the uncomfortable thing. A practitioner who did the thing, not a thought leader performing.

WRITE ONE TWEET. Hard rules:
- MAX 280 characters total. This is absolute. Shorter is fine and often better.
- First line is a hook that stops the scroll: a concrete claim, number, or moment. Never a question as the whole hook.
- Write in full, flowing sentences. NEVER use short chopped fragment sentences for effect (no "Not a visit. To stay." style). This is critical.
- Specific beats general. Real experience beats theory.
- No hashtags. No emoji. No "I'm excited to share". No dashes or em-dashes (use comma or period).
- Do not mention the current company by name (say "what I'm building"). Never say "sold to Fiverr".
- Do not mention you're an AI.
- One tweet only, no thread, no numbering, no quotes around it. Output ONLY the tweet text."""


def generate_tweet(recent_texts=None, kb_context: str = "") -> str:
    """Generate one original X post (<=280 chars) in Nick's voice, guided by viral
    patterns. Returns the tweet text. Tries a few times to satisfy the length cap."""
    try:
        from analyze_viral_posts import load_patterns_for_prompt
        viral_patterns = load_patterns_for_prompt()
    except Exception:
        viral_patterns = ""
    try:
        from track_own_posts import load_own_posts_for_prompt
        own_posts_context = load_own_posts_for_prompt()
    except Exception:
        own_posts_context = ""

    kb_text = f"# Nick's Knowledge Base\n\n{kb_context}" if kb_context else "# Nick"
    if viral_patterns:
        kb_text += f"\n\n---\n\n{viral_patterns}"
    if own_posts_context:
        kb_text += f"\n\n---\n\n{own_posts_context}"

    avoid = ""
    if recent_texts:
        joined = "\n".join(f"- {t[:120]}" for t in recent_texts[-15:])
        avoid = (f"\n\nDo NOT repeat the topic, hook, or angle of these recent tweets:\n{joined}")

    cached_kb = [{"type": "text", "text": kb_text, "cache_control": {"type": "ephemeral"}}]
    user_text = (
        "Write one original tweet for Nick today. Draw on his real experience and the viral "
        "patterns (proven to perform). Make it something only he could write, not generic "
        "founder content. Vary the angle from his past posts." + avoid
    )

    for _ in range(3):
        resp = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=TWEET_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": cached_kb + [{"type": "text", "text": user_text}]}],
        )
        text = resp.content[0].text.strip().strip('"').strip()
        text = text.replace(" — ", ", ").replace(" – ", ", ").replace("—", ", ").replace("–", ", ")
        if 0 < len(text) <= 280:
            return text
    # Last resort: hard cap at the last sentence boundary under 280.
    text = text[:280]
    cut = max(text.rfind(". "), text.rfind("\n"))
    return text[:cut + 1].strip() if cut > 100 else text.strip()


def extract_trending_topics(posts: list[dict]) -> list[str]:
    """Pull top themes from fetched posts by looking at high-engagement content."""
    sorted_posts = sorted(posts, key=lambda x: x.get("likes", 0), reverse=True)
    topics = []
    for p in sorted_posts[:15]:
        text = p.get("text", "")[:120].strip().replace("\n", " ")
        likes = p.get("likes", 0)
        topics.append(f"{text} ({likes} likes)")
    return topics
