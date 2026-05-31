"""
Analyzes accumulated viral tweets from viral_tweets_db.json,
extracts patterns (themes, hooks, tone, media impact),
and saves them to viral_tweet_patterns_db.json.

Runs automatically when 10+ new tweets have accumulated since the last analysis.
"""
import json
import os
import re
from datetime import datetime, timezone

import anthropic
from config import ANTHROPIC_API_KEY, DATA_DIR

VIRAL_TWEETS_PATH = os.path.join(DATA_DIR, "viral_tweets_db.json")
PATTERNS_PATH = os.path.join(DATA_DIR, "viral_tweet_patterns_db.json")

MIN_NEW_TWEETS = 10

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ANALYSIS_PROMPT = """You are analyzing a set of high-engagement tweets to extract virality patterns for a founder audience.

Each tweet is shown as: likes L, replies R, retweets RT, [content_type] | text

Return a JSON object with exactly these keys:

{
  "top_themes": [
    {"theme": "...", "description": "...", "example_hook": "..."}
  ],
  "hook_patterns": [
    {"type": "...", "pattern": "...", "example": "..."}
  ],
  "tone_insights": {
    "best_tone": "...",
    "length": "short|medium",
    "thread_vs_single": "..."
  },
  "media_impact": "Does including images/video increase engagement? What type works best?",
  "what_drives_replies": "What specifically makes people reply (not just like or retweet)?",
  "summary": "2-3 sentence synthesis of what makes tweets viral in this feed right now"
}

top_themes: 4-6 themes with short label, description, and sample hook.
hook_patterns: 4-5 hook archetypes (e.g. "hot take", "data drop", "personal failure", "contrarian").
tone_insights: observations about tone, length, thread vs single tweet.
media_impact: observation about whether images/video meaningfully boost engagement.
what_drives_replies: what makes people engage in conversation vs just like.
summary: key takeaway in plain language.

Return only valid JSON. No markdown, no explanation."""


def _load_viral_tweets() -> list[dict]:
    if not os.path.exists(VIRAL_TWEETS_PATH):
        return []
    with open(VIRAL_TWEETS_PATH) as f:
        return json.load(f)


def _load_patterns() -> dict:
    if not os.path.exists(PATTERNS_PATH):
        return {}
    with open(PATTERNS_PATH) as f:
        return json.load(f)


def _should_run(tweets: list[dict], patterns: dict) -> bool:
    if not patterns:
        return len(tweets) >= MIN_NEW_TWEETS
    return (len(tweets) - patterns.get("tweets_analyzed", 0)) >= MIN_NEW_TWEETS


def _build_tweets_text(tweets: list[dict]) -> str:
    sorted_tweets = sorted(
        tweets,
        key=lambda t: t.get("likes", 0) + 3 * t.get("replies", 0),
        reverse=True,
    )
    lines = []
    for i, t in enumerate(sorted_tweets[:60], 1):
        likes = t.get("likes", 0)
        replies = t.get("replies", 0)
        rts = t.get("retweets", 0)
        ctype = t.get("content_type", "text")
        text = t.get("text", "").replace("\n", " ").strip()[:300]
        lines.append(f"[{i}] {likes}L {replies}R {rts}RT [{ctype}] | {text}")
    return "\n\n".join(lines)


def run(force: bool = False) -> "dict | None":
    tweets = _load_viral_tweets()
    patterns = _load_patterns()

    if not force and not _should_run(tweets, patterns):
        return None

    if len(tweets) < 5:
        return None

    print(f"  Analyzing {len(tweets)} viral tweets for patterns...")

    tweets_text = _build_tweets_text(tweets)

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": (
                f"{ANALYSIS_PROMPT}\n\n"
                f"Here are {len(tweets)} high-engagement tweets:\n\n{tweets_text}"
            ),
        }],
    )

    raw = response.content[0].text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            print("  Tweet pattern analysis: failed to parse response.")
            return None
        result = json.loads(m.group(0))

    result["tweets_analyzed"] = len(tweets)
    result["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(PATTERNS_PATH, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  Viral tweet patterns updated → viral_tweet_patterns_db.json ({len(tweets)} tweets)")
    return result


def save_viral_tweet(tweet: dict, our_reply: str):
    """Save a tweet we replied to into the viral tweets DB."""
    if os.path.exists(VIRAL_TWEETS_PATH):
        try:
            with open(VIRAL_TWEETS_PATH) as f:
                db = json.load(f)
        except Exception:
            db = []
    else:
        db = []

    existing_urls = {t["url"] for t in db}
    if tweet.get("url") in existing_urls:
        return

    db.append({
        "url": tweet.get("url", ""),
        "tweet_id": tweet.get("tweet_id", ""),
        "author": tweet.get("author", ""),
        "author_username": tweet.get("author_username", ""),
        "text": tweet.get("text", ""),
        "likes": tweet.get("likes", 0),
        "replies": tweet.get("replies", 0),
        "retweets": tweet.get("retweets", 0),
        "content_type": tweet.get("content_type", "text"),
        "posted_at": tweet.get("posted_at", ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "our_reply": our_reply,
    })

    db.sort(key=lambda t: t.get("likes", 0) + 3 * t.get("replies", 0), reverse=True)

    with open(VIRAL_TWEETS_PATH, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def load_patterns_for_prompt() -> str:
    """Return formatted patterns string for injection into reply generation prompt."""
    patterns = _load_patterns()
    if not patterns:
        return ""

    lines = ["## Viral Patterns in Nick's Twitter Feed\n"]

    if patterns.get("summary"):
        lines.append(f"**Summary:** {patterns['summary']}\n")

    for t in patterns.get("top_themes", []):
        pass  # included below

    themes = patterns.get("top_themes", [])
    if themes:
        lines.append("**Top themes:**")
        for t in themes:
            lines.append(f"- {t.get('theme','')}: {t.get('description','')} | Hook: \"{t.get('example_hook','')}\"")
        lines.append("")

    hooks = patterns.get("hook_patterns", [])
    if hooks:
        lines.append("**Hook patterns:**")
        for h in hooks:
            lines.append(f"- {h.get('type','')}: {h.get('pattern','')} | E.g. \"{h.get('example','')}\"")
        lines.append("")

    media = patterns.get("media_impact", "")
    if media:
        lines.append(f"**Media impact:** {media}\n")

    replies_driver = patterns.get("what_drives_replies", "")
    if replies_driver:
        lines.append(f"**What drives replies:** {replies_driver}\n")

    updated = patterns.get("last_updated", "")[:10]
    count = patterns.get("tweets_analyzed", 0)
    lines.append(f"_Based on {count} tweets, updated {updated}_")

    return "\n".join(lines)


if __name__ == "__main__":
    result = run(force=True)
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        tweets = _load_viral_tweets()
        print(f"Skipped: {len(tweets)} tweets, need {MIN_NEW_TWEETS}+ new since last analysis.")
