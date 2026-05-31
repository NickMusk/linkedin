"""
Analyzes accumulated viral posts from viral_posts_db.json,
extracts patterns (themes, hooks, structure, emotional triggers),
and saves them to viral_patterns_db.json.

Runs automatically when 10+ new posts have accumulated since the last analysis.
"""
import json
import os
from datetime import datetime, timezone

import anthropic
from config import ANTHROPIC_API_KEY, DATA_DIR

VIRAL_POSTS_PATH = os.path.join(DATA_DIR, "viral_posts_db.json")
PATTERNS_PATH = os.path.join(DATA_DIR, "viral_patterns_db.json")

MIN_NEW_POSTS = 10  # run analysis only when this many new posts accumulated

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ANALYSIS_PROMPT = """You are analyzing a set of high-engagement LinkedIn posts to extract virality patterns.

For each pattern, be specific and actionable — avoid vague advice like "be authentic".

Return a JSON object with exactly these keys:

{
  "top_themes": [
    {"theme": "...", "description": "...", "example_hook": "..."}
  ],
  "hook_patterns": [
    {"type": "...", "pattern": "...", "example": "..."}
  ],
  "structure_insights": {
    "avg_length": "short|medium|long",
    "best_formats": ["..."],
    "paragraph_style": "...",
    "list_vs_prose": "..."
  },
  "emotional_triggers": ["...", "..."],
  "what_drives_comments": "...",
  "content_type_performance": {
    "best_type": "image|document|text|video",
    "observation": "..."
  },
  "summary": "2-3 sentence synthesis of what makes posts viral in this feed right now"
}

top_themes: 5-7 themes. Each theme has a short label, description, and a sample hook sentence.
hook_patterns: 4-6 hook archetypes seen in the top posts (e.g. "contrarian claim", "specific number", "personal failure").
structure_insights: observations about length, formatting, use of lists.
emotional_triggers: 3-5 emotions these posts reliably trigger.
what_drives_comments: what specifically makes people comment (not just like).
content_type_performance: which content type (image/document/text/video) gets the most engagement and why.
summary: the key takeaway in plain language.

Return only valid JSON. No markdown fences, no explanation outside the JSON."""


def _load_viral_posts() -> list[dict]:
    if not os.path.exists(VIRAL_POSTS_PATH):
        return []
    with open(VIRAL_POSTS_PATH) as f:
        return json.load(f)


def _load_patterns() -> dict:
    if not os.path.exists(PATTERNS_PATH):
        return {}
    with open(PATTERNS_PATH) as f:
        return json.load(f)


def _should_run(posts: list[dict], patterns: dict) -> bool:
    if not patterns:
        return len(posts) >= MIN_NEW_POSTS
    last_count = patterns.get("posts_analyzed", 0)
    return (len(posts) - last_count) >= MIN_NEW_POSTS


def _build_posts_text(posts: list[dict]) -> str:
    """Format top posts for analysis. Use top 60 by engagement_score."""
    sorted_posts = sorted(posts, key=lambda p: p.get("engagement_score", p.get("likes", 0)), reverse=True)
    lines = []
    for i, p in enumerate(sorted_posts[:60], 1):
        likes = p.get("likes", 0)
        comments = p.get("comments", 0)
        ctype = p.get("content_type", "text")
        text = p.get("text", "").replace("\n", " ").strip()[:400]
        lines.append(f"[{i}] {likes}L {comments}C [{ctype}] | {text}")
    return "\n\n".join(lines)


def run_analysis(force: bool = False) -> "dict | None":
    """
    Run pattern analysis if enough new posts have accumulated.
    Returns the patterns dict if analysis ran, None if skipped.
    """
    posts = _load_viral_posts()
    patterns = _load_patterns()

    if not force and not _should_run(posts, patterns):
        return None

    if len(posts) < 5:
        return None

    print(f"  Analyzing {len(posts)} viral posts for patterns...")

    posts_text = _build_posts_text(posts)

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{ANALYSIS_PROMPT}\n\n"
                    f"Here are {len(posts)} high-engagement LinkedIn posts (format: likes L, comments C | text):\n\n"
                    f"{posts_text}"
                ),
            }
        ],
    )

    raw = response.content[0].text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON if there's surrounding text
        import re
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            print("  Pattern analysis: failed to parse Claude response.")
            return None
        result = json.loads(m.group(0))

    result["posts_analyzed"] = len(posts)
    result["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(PATTERNS_PATH, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  Viral patterns updated → viral_patterns_db.json ({len(posts)} posts analyzed)")
    return result


def load_patterns_for_prompt() -> str:
    """Return a formatted string of viral patterns for injection into post generation prompt."""
    patterns = _load_patterns()
    if not patterns:
        return ""

    lines = ["## Viral Patterns in Nick's Feed (from analysis of top posts)\n"]

    summary = patterns.get("summary", "")
    if summary:
        lines.append(f"**Summary:** {summary}\n")

    themes = patterns.get("top_themes", [])
    if themes:
        lines.append("**Top themes that perform well:**")
        for t in themes:
            lines.append(f"- {t.get('theme', '')}: {t.get('description', '')} | Hook example: \"{t.get('example_hook', '')}\"")
        lines.append("")

    hooks = patterns.get("hook_patterns", [])
    if hooks:
        lines.append("**Hook archetypes that stop scrolling:**")
        for h in hooks:
            lines.append(f"- {h.get('type', '')}: {h.get('pattern', '')} | E.g. \"{h.get('example', '')}\"")
        lines.append("")

    triggers = patterns.get("emotional_triggers", [])
    if triggers:
        lines.append(f"**Emotional triggers:** {', '.join(triggers)}\n")

    comments_driver = patterns.get("what_drives_comments", "")
    if comments_driver:
        lines.append(f"**What drives comments:** {comments_driver}\n")

    updated = patterns.get("last_updated", "")[:10]
    count = patterns.get("posts_analyzed", 0)
    lines.append(f"_Patterns based on {count} posts, last updated {updated}_")

    return "\n".join(lines)


if __name__ == "__main__":
    result = run_analysis(force=True)
    if result:
        print("\nExtracted patterns:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        posts = _load_viral_posts()
        print(f"Skipped: {len(posts)} posts in DB, need {MIN_NEW_POSTS}+ new since last analysis.")
