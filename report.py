import os
import re
from datetime import datetime
from pathlib import Path
from config import DATA_DIR


def session_dir() -> str:
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    d = os.path.join(DATA_DIR, "reports", ts)
    os.makedirs(d, exist_ok=True)
    return d


def save_posts(posts: list[dict], directory: str) -> str:
    path = os.path.join(directory, "posts.md")
    lines = [f"# Posts — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    for i, p in enumerate(posts, 1):
        lines.append(f"## {i}. {p['author']}")
        lines.append(f"**Role:** {p['author_title']}")
        lines.append(f"**Engagement:** {p['likes']} likes · {p['comments']} comments")
        lines.append(f"**Source:** {p['source']}")
        lines.append(f"**URL:** {p['url']}\n")
        lines.append(p['text'][:800] + ("..." if len(p['text']) > 800 else ""))
        lines.append("\n---\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def save_comments(items: list[dict], directory: str) -> str:
    path = os.path.join(directory, "comments.md")
    lines = [
        f"# Comment Drafts — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
        "Instructions: Set STATUS to `approved` or `rejected`. Add FEEDBACK for revisions.\n",
        "---\n",
    ]
    for i, item in enumerate(items, 1):
        lines.append(f"## {i}. {item['author']}")
        lines.append(f"**URL:** {item['url']}")
        lines.append(f"**Engagement:** {item['likes']} likes · {item['comments']} comments\n")
        lines.append("**Post excerpt:**")
        lines.append(f"> {item['text'][:300].strip().replace(chr(10), ' ')}\n")
        lines.append("**Draft comment:**")
        lines.append(f"```\n{item['draft']}\n```\n")
        lines.append(f"**Reasoning:** {item['reasoning']}\n")
        status = "rejected" if item.get("skip") else "pending"
        lines.append(f"**STATUS:** {status}")
        lines.append("**FEEDBACK:**\n")
        lines.append("---\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def parse_approved(directory: str) -> list[dict]:
    """Parse comments.md — returns approved items with optional final override."""
    path = os.path.join(directory, "comments.md")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No comments.md in {directory}")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r"\n---\n", content)
    approved = []

    for block in blocks:
        if "**STATUS:** approved" not in block:
            continue

        url_m = re.search(r"\*\*URL:\*\* (.+)", block)
        author_m = re.search(r"^## \d+\. (.+)", block, re.MULTILINE)
        draft_m = re.search(r"```\n([\s\S]+?)\n```", block)
        feedback_m = re.search(r"\*\*FEEDBACK:\*\*\s*\n([\s\S]+?)(?=\n\*\*|$)", block)
        final_m = re.search(r"\*\*FINAL:\*\*\s*\n```\n([\s\S]+?)\n```", block)
        excerpt_m = re.search(r"\*\*Post excerpt:\*\*\n> (.+)", block)

        if not (url_m and draft_m):
            continue

        approved.append({
            "url": url_m.group(1).strip(),
            "author": author_m.group(1).strip() if author_m else "",
            "draft": draft_m.group(1).strip(),
            "feedback": feedback_m.group(1).strip() if feedback_m else "",
            "final": final_m.group(1).strip() if final_m else "",
            "text": excerpt_m.group(1).strip() if excerpt_m else "",
        })

    return approved


def find_latest_session():
    reports_dir = os.path.join(DATA_DIR, "reports")
    sessions = sorted([
        d for d in Path(reports_dir).iterdir() if d.is_dir()
    ], reverse=True)
    return str(sessions[0]) if sessions else None
