import os
import json
from datetime import datetime, timezone
from config import DATA_DIR

KB_PATH = os.path.join(os.path.dirname(__file__), "nick_interview.md")
EXAMPLES_PATH = os.path.join(DATA_DIR, "comment_examples.md")


def build_context(kb_path: str = None) -> str:
    path = kb_path or KB_PATH
    parts = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            parts.append(f.read())
    except Exception as e:
        parts.append(f"[Knowledge base unavailable: {e}]")

    if os.path.exists(EXAMPLES_PATH):
        try:
            with open(EXAMPLES_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(content)
        except Exception:
            pass

    return "\n\n---\n\n".join(parts)


# Cap the examples file so the cached KB prefix stops growing without bound.
# Every cold run pays the full write cost for this content, so keep it to the
# most recent N calibration examples.
MAX_EXAMPLES = 40


def save_example(post_excerpt: str, comment: str):
    """Append a published LinkedIn post+comment pair, keeping only the last MAX_EXAMPLES."""
    entry = f"POST:\n> {post_excerpt[:300].strip()}\n\nCOMMENT:\n{comment}\n\n---\n\n"
    existing = ""
    if os.path.exists(EXAMPLES_PATH):
        try:
            with open(EXAMPLES_PATH, "r", encoding="utf-8") as f:
                existing = f.read()
        except Exception:
            existing = ""

    entries = [e for e in existing.split("\n\n---\n\n") if e.strip()]
    entries.append(entry.strip())
    entries = entries[-MAX_EXAMPLES:]

    with open(EXAMPLES_PATH, "w", encoding="utf-8") as f:
        f.write("\n\n---\n\n".join(entries) + "\n\n---\n\n")


VIRAL_POSTS_PATH = os.path.join(DATA_DIR, "viral_posts_db.json")
TWEET_EXAMPLES_PATH = os.path.join(DATA_DIR, "tweet_examples.md")


def save_viral_post(post: dict, our_comment: str):
    """Save a high-engagement LinkedIn post we commented on to the viral posts DB."""
    if os.path.exists(VIRAL_POSTS_PATH):
        try:
            with open(VIRAL_POSTS_PATH) as f:
                db = json.load(f)
        except Exception:
            db = []
    else:
        db = []

    existing_urls = {e["url"] for e in db}
    if post["url"] in existing_urls:
        return

    db.append({
        "url": post["url"],
        "author": post.get("author", ""),
        "author_title": post.get("author_title", ""),
        "text": post.get("text", ""),
        "likes": post.get("likes", 0),
        "comments": post.get("comments", 0),
        "engagement_score": post.get("engagement_score", 0),
        "posted_at": post.get("posted_at", ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "our_comment": our_comment,
    })

    db.sort(key=lambda e: e.get("engagement_score", 0), reverse=True)

    with open(VIRAL_POSTS_PATH, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def save_tweet_example(tweet_text: str, reply: str):
    """Append an approved tweet+reply pair to the tweet examples file."""
    if not os.path.exists(TWEET_EXAMPLES_PATH):
        with open(TWEET_EXAMPLES_PATH, "w", encoding="utf-8") as f:
            f.write("# Tweet Reply Examples — Nick's Approved Replies\n\nUse these to calibrate voice and length for Twitter.\n\n---\n\n")
    entry = f"TWEET:\n> {tweet_text[:280].strip()}\n\nREPLY:\n{reply}\n\n---\n\n"
    with open(TWEET_EXAMPLES_PATH, "a", encoding="utf-8") as f:
        f.write(entry)
