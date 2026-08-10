import re
import random
import logging
import anthropic
from config import ANTHROPIC_API_KEY
from knowledge_base import build_context

log = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are Nick Nagatkin's Twitter reply writer.

Nick's background: sold an IT staffing company at 30, built it from 5 to 150 people, navigated COVID by hiring aggressively when competitors cut, took a 30% revenue hit the day Ukraine war started and survived. Now building an AI venture in stealth, pre-seed fundraising. Lives in Dubai.

Twitter reply style — different from LinkedIn:
- Twitter is shorter and punchier. 1-2 sentences MAX. Often just 1.
- More casual, less polished. Can start with "lol", "yeah", "this", "honestly", "hard agree" etc.
- Wit and irony land better here than on LinkedIn
- Can be a direct pushback without softening
- Numbers and specifics still win
- No hashtags. No emojis unless it's a single one that earns it.
- Self-deprecating humor works well
- Don't start with "Great tweet" or any compliment
- Replies that add a contrasting data point or a "yeah but" do better than pure agreement

LOW-EFFORT MODE:
Some requests explicitly ask for a LOW-EFFORT reply. Those are required for the account to feel human, natural accounts alternate thoughtful replies with throwaway reactions. In low-effort mode:
- Output only a lightweight reaction: "exactly", "lol", "this", "brutal", "yep", "100%", "wild", "true", "haha", "love this", a very short question, a one-line joke, a dry reaction, or a 2-5 word observation
- It does NOT need to add information, never append an explanation or insight after it
- GOOD: "brutal"  BAD: "brutal. this really shows how difficult distribution becomes as companies scale"
- GOOD: "lol exactly"  BAD: "lol exactly. founders consistently underestimate this dynamic"
- GOOD: "does this hold for enterprise?"  BAD: "interesting. does this hold for enterprise? I've found enterprise buyers behave differently"
- All SKIP rules below still apply

HARD RULES:
- Max 280 characters ideally, never over 400
- No em-dashes, hyphens between words. Use comma or period.
- No credential flex ("after 5000 hires", "when I sold my company")
- Never quote the author's phrase back at them
- If the tweet is a job posting or promotional content, output exactly: SKIP
- If the tweet mentions Fiverr or is from/about Fiverr, output exactly: SKIP
- Never end the reply with a period
- Output ONLY the reply text. Nothing else. Never prefix it with a label, header, or mode name (never output the words "LOW EFFORT")
"""


def generate_replies(tweets: list[dict], kb_context: str) -> list[dict]:
    try:
        from analyze_viral_tweets import load_patterns_for_prompt
        tweet_patterns = load_patterns_for_prompt()
    except Exception:
        tweet_patterns = ""

    kb_text = f"# Nick's Knowledge Base\n\n{kb_context}"
    if tweet_patterns:
        kb_text += f"\n\n---\n\n{tweet_patterns}"

    cached_kb = [
        {
            "type": "text",
            "text": kb_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    results = []
    for i, tweet in enumerate(tweets):
        # ~25% of replies must be throwaway reactions ("lol", "brutal") — an
        # all-insightful account reads as generated. Decided here, not by the
        # model: each reply is a separate API call, so the model can't track
        # the distribution itself.
        low_effort = random.random() < 0.25
        print(f"  Generating reply {i+1}/{len(tweets)}{' (low-effort)' if low_effort else ''}: @{tweet.get('author_username', tweet['author'])[:25]}")
        draft = _generate_one(tweet, cached_kb, low_effort)
        skip = draft.strip().upper() == "SKIP"
        results.append({**tweet, "draft": draft, "skip": skip, "low_effort": low_effort})
    return results


def _build_image_content(image_url: str) -> list:
    """Download and return base64 image content block, or empty list on failure."""
    if not image_url:
        return []
    try:
        import requests as _req, base64
        r = _req.get(image_url, timeout=8)
        if r.status_code != 200 or not r.content:
            return []
        media_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        b64 = base64.standard_b64encode(r.content).decode("utf-8")
        return [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}]
    except Exception:
        return []


def _generate_one(tweet: dict, cached_kb: list, low_effort: bool = False) -> str:
    content_type = tweet.get("content_type", "text")
    instruction = (
        "Write a LOW-EFFORT Twitter reply for this tweet (see LOW-EFFORT MODE: throwaway reaction, short question, or one-line joke, no added insight):"
        if low_effort
        else "Write a Twitter reply for this tweet:"
    )
    tweet_block = (
        f"@{tweet.get('author_username', '')} ({tweet['author']})\n"
        f"Likes: {tweet['likes']} | Replies: {tweet.get('replies', 0)} | Type: {content_type}\n\n"
        f"{tweet['text']}"
    )

    user_content = cached_kb[:]
    image_blocks = _build_image_content(tweet.get("image_url", "")) if content_type == "image" else []
    if image_blocks:
        user_content += image_blocks
        user_content.append({
            "type": "text",
            "text": f"The image above is attached to this tweet. Use it if relevant.\n\n{instruction}\n\n{tweet_block}",
        })
    else:
        user_content.append({
            "type": "text",
            "text": f"{instruction}\n\n{tweet_block}",
        })

    try:
        response = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return _strip_dashes(response.content[0].text.strip())
    except Exception as e:
        log.warning(f"  [reply API error] {e}")
        return "SKIP"


def _strip_dashes(text: str) -> str:
    # The system prompt describes a "LOW-EFFORT MODE"; the model occasionally
    # leaks that name as a header on the reply. Posting it would expose the bot.
    text = re.sub(r'^\s*LOW[ -]?EFFORT( MODE)?[:.\s]*\n+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*—\s*', ', ', text)
    text = re.sub(r'\s*–\s*', ', ', text)
    text = re.sub(r'\s*--\s*', ', ', text)
    text = re.sub(r'(?<=[a-zA-Z])-(?=[a-zA-Z])', ' ', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'^\s*,\s*', '', text)
    # Remove period before emoticons and at end of line
    text = re.sub(r'\.\s*([;:]\))', r' \1', text)
    text = re.sub(r'\.\s*$', '', text, flags=re.MULTILINE)
    return text.strip()
