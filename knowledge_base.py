import os
from config import DATA_DIR

KB_PATH = os.path.join(os.path.dirname(__file__), "nick_interview.md")
EXAMPLES_PATH = os.path.join(DATA_DIR, "comment_examples.md")


def build_context() -> str:
    parts = []
    try:
        with open(KB_PATH, "r", encoding="utf-8") as f:
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


def save_example(post_excerpt: str, comment: str):
    """Append a published LinkedIn post+comment pair to the examples file."""
    entry = f"POST:\n> {post_excerpt[:300].strip()}\n\nCOMMENT:\n{comment}\n\n---\n\n"
    with open(EXAMPLES_PATH, "a", encoding="utf-8") as f:
        f.write(entry)


TWEET_EXAMPLES_PATH = os.path.join(DATA_DIR, "tweet_examples.md")


def save_tweet_example(tweet_text: str, reply: str):
    """Append an approved tweet+reply pair to the tweet examples file."""
    if not os.path.exists(TWEET_EXAMPLES_PATH):
        with open(TWEET_EXAMPLES_PATH, "w", encoding="utf-8") as f:
            f.write("# Tweet Reply Examples — Nick's Approved Replies\n\nUse these to calibrate voice and length for Twitter.\n\n---\n\n")
    entry = f"TWEET:\n> {tweet_text[:280].strip()}\n\nREPLY:\n{reply}\n\n---\n\n"
    with open(TWEET_EXAMPLES_PATH, "a", encoding="utf-8") as f:
        f.write(entry)
