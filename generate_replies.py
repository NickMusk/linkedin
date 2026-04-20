import re
import anthropic
from config import ANTHROPIC_API_KEY
from knowledge_base import build_context

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

HARD RULES:
- Max 280 characters ideally, never over 400
- No em-dashes, hyphens between words. Use comma or period.
- No credential flex ("after 5000 hires", "when I sold my company")
- Never quote the author's phrase back at them
- If the tweet is a job posting or promotional content, output exactly: SKIP
- If the tweet mentions Fiverr or is from/about Fiverr, output exactly: SKIP
- Never end the reply with a period
- Output ONLY the reply text. Nothing else.
"""


def generate_replies(tweets: list[dict], kb_context: str) -> list[dict]:
    results = []
    cached_kb = [
        {
            "type": "text",
            "text": f"# Nick's Knowledge Base\n\n{kb_context}",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    for i, tweet in enumerate(tweets):
        print(f"  Generating reply {i+1}/{len(tweets)}: @{tweet.get('author_username', tweet['author'])[:25]}")
        draft = _generate_one(tweet, cached_kb)
        skip = draft.strip().upper() == "SKIP"
        results.append({**tweet, "draft": draft, "skip": skip})
    return results


def _generate_one(tweet: dict, cached_kb: list) -> str:
    tweet_block = (
        f"@{tweet.get('author_username', '')} ({tweet['author']})\n"
        f"Likes: {tweet['likes']} | Replies: {tweet.get('replies', 0)}\n\n"
        f"{tweet['text']}"
    )
    try:
        response = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": cached_kb + [{
                    "type": "text",
                    "text": f"Write a Twitter reply for this tweet:\n\n{tweet_block}"
                }]
            }],
        )
        return _strip_dashes(response.content[0].text.strip())
    except Exception as e:
        return f"[Error: {e}]"


def _strip_dashes(text: str) -> str:
    text = re.sub(r'\s*—\s*', ', ', text)
    text = re.sub(r'\s*–\s*', ', ', text)
    text = re.sub(r'\s*--\s*', ', ', text)
    text = re.sub(r'(?<=[a-zA-Z])-(?=[a-zA-Z])', ' ', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'^\s*,\s*', '', text)
    return text.strip()
