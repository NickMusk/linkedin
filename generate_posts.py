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

    cached_kb = [
        {
            "type": "text",
            "text": f"# Nick's Knowledge Base\n\n{kb_context}",
            "cache_control": {"type": "ephemeral"},
        }
    ]

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
                        "text": (
                            f"Trending topics in Nick's LinkedIn feed right now:\n{topics_text}\n\n"
                            f"Write 3 LinkedIn post drafts for Nick. Draw on his real experience from the knowledge base. "
                            f"Each post should feel like something only he could write, not generic founder content."
                        ),
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


def extract_trending_topics(posts: list[dict]) -> list[str]:
    """Pull top themes from fetched posts by looking at high-engagement content."""
    sorted_posts = sorted(posts, key=lambda x: x.get("likes", 0), reverse=True)
    topics = []
    for p in sorted_posts[:15]:
        text = p.get("text", "")[:120].strip().replace("\n", " ")
        likes = p.get("likes", 0)
        topics.append(f"{text} ({likes} likes)")
    return topics
