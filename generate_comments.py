import re
import anthropic
from config import ANTHROPIC_API_KEY
from knowledge_base import build_context

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
- NEVER comment on job postings. If the post is primarily a hiring announcement or job description, output exactly: SKIP
- If the post is NOT written in English (e.g. Russian, Ukrainian, Hebrew, Spanish, etc.), output exactly: SKIP
- NEVER open by quoting the author's phrase back at them in quotation marks. No "The 'X framing' is real but...", no "The 'Y model' works until...", no "The 'Z line' is right but...". State your counter or observation directly without echoing their words.
- NEVER comment on posts where the author is primarily promoting their own product, service, or company (product launches, feature announcements, "we just shipped X", "check out what we built"). These are advertisements, not opinions. Output exactly: SKIP
- NEVER comment on personal career milestone posts from people Nick doesn't know personally (e.g. "excited to share I've joined X", "thrilled to announce my promotion to Y", "I've been promoted to Z"). Output exactly: SKIP
"""


def generate_comments(posts: list[dict], kb_context: str) -> list[dict]:
    results = []

    # Cache the knowledge base context across all calls
    cached_kb = [
        {
            "type": "text",
            "text": f"# Nick's Knowledge Base\n\n{kb_context}",
            "cache_control": {"type": "ephemeral"},
        }
    ]

    for i, post in enumerate(posts):
        print(f"  Generating comment {i+1}/{len(posts)}: {post['author'][:30]}")
        draft, reasoning = _generate_one(post, cached_kb)
        skip = draft.strip().upper() == "SKIP"
        results.append({**post, "draft": draft, "reasoning": reasoning, "skip": skip})

    return results


def _generate_one(post: dict, cached_kb: list) -> tuple[str, str]:
    score = post.get("engagement_score", post["likes"] + 3 * post["comments"])
    posted_at = post.get("posted_at", "")
    age_note = f" | Posted: {posted_at[:16].replace('T', ' ')} UTC" if posted_at else ""
    post_block = (
        f"Author: {post['author']} — {post['author_title']}\n"
        f"Likes: {post['likes']} | Comments: {post['comments']} | Engagement score: {score}{age_note}\n"
        f"URL: {post['url']}\n\n"
        f"{post['text']}"
    )

    try:
        response = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": cached_kb + [
                        {
                            "type": "text",
                            "text": (
                                f"Write a LinkedIn comment for this post. "
                                f"Then on a new line starting with 'REASONING:' explain in 1-2 sentences "
                                f"which specific experience/quote from the knowledge base you drew on and why.\n\n"
                                f"POST:\n{post_block}"
                            ),
                        }
                    ],
                }
            ],
        )
    except Exception as e:
        return f"[Error generating comment: {e}]", ""

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
    # Remove trailing period from each paragraph
    text = re.sub(r'\.\s*$', '', text, flags=re.MULTILINE)
    return text.strip()
