#!/usr/bin/env python3
"""
LinkedIn Comment Assistant

Commands:
  python run.py fetch      — fetch LinkedIn posts → reports/<session>/posts.md
  python run.py generate   — build KB context + generate drafts → comments.md
  python run.py refine     — re-generate comments based on FEEDBACK in comments.md
  python run.py publish    — post STATUS:approved comments via Buffer
  python run.py full       — fetch + generate in one shot
"""
import sys
import os
import json


def cmd_fetch():
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts

    posts = fetch_all_posts()
    if not posts:
        print("No posts found.")
        return

    d = session_dir()
    path = save_posts(posts, d)
    print(f"\nSaved {len(posts)} posts → {path}")
    print(f"Session: {d}")


def cmd_generate(session = None):
    from report import find_latest_session, save_comments
    from knowledge_base import build_context
    from generate_comments import generate_comments
    import json

    d = session or find_latest_session()
    if not d:
        print("No session found. Run 'fetch' first.")
        return

    posts_path = os.path.join(d, "posts.md")
    if not os.path.exists(posts_path):
        print(f"No posts.md in {d}")
        return

    # Re-load posts from session (stored as md, we reload from raw json if exists)
    raw_json = os.path.join(d, "posts.json")
    if os.path.exists(raw_json):
        with open(raw_json) as f:
            posts = json.load(f)
    else:
        print("No posts.json found. Run 'fetch' first (it saves raw JSON).")
        return

    print("Building knowledge base context...")
    kb = build_context()
    print(f"KB context: {len(kb):,} chars")

    print("Generating comments...")
    items = generate_comments(posts, kb)

    path = save_comments(items, d)
    print(f"\nSaved {len(items)} drafts → {path}")
    print("\nReview comments.md, set STATUS to 'approved' or 'rejected', add FEEDBACK if needed.")
    print("Then run: python run.py refine   (if feedback given)")
    print("     or:  python run.py publish  (if ready)")


def cmd_refine(session = None):
    """Re-generate comments where FEEDBACK is present and status is not yet approved."""
    from report import find_latest_session, parse_approved, save_comments
    from knowledge_base import build_context
    from generate_comments import _generate_one, _client
    import re

    d = session or find_latest_session()
    if not d:
        print("No session found.")
        return

    path = os.path.join(d, "comments.md")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    print("Building knowledge base context...")
    kb = build_context()

    cached_kb = [
        {
            "type": "text",
            "text": f"# Nick's Knowledge Base\n\n{kb}",
            "cache_control": {"type": "ephemeral"},
        }
    ]

    blocks = content.split("\n---\n")
    updated_blocks = []

    for block in blocks:
        if "**FEEDBACK:**" in block and "**STATUS:** pending" in block:
            feedback_m = re.search(r"\*\*FEEDBACK:\*\*\s*\n(.+)", block)
            url_m = re.search(r"\*\*URL:\*\* (.+)", block)
            text_m = re.search(r"> (.+)", block)
            draft_m = re.search(r"```\n([\s\S]+?)\n```", block)

            if feedback_m and feedback_m.group(1).strip() and url_m and draft_m:
                feedback = feedback_m.group(1).strip()
                post_text = text_m.group(1).strip() if text_m else ""
                old_draft = draft_m.group(1).strip()
                url = url_m.group(1).strip()

                print(f"  Refining: {url[:60]}")
                post = {"url": url, "author": "", "author_title": "", "text": post_text,
                        "likes": 0, "comments": 0}

                # Inject feedback into prompt
                import anthropic
                from config import ANTHROPIC_API_KEY
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                from config import ANTHROPIC_API_KEY
                from generate_comments import SYSTEM_PROMPT

                resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=600,
                    system=SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": cached_kb + [{
                            "type": "text",
                            "text": (
                                f"Rewrite this LinkedIn comment based on the feedback.\n\n"
                                f"Original comment:\n{old_draft}\n\n"
                                f"Feedback: {feedback}\n\n"
                                f"Post context: {post_text}\n\n"
                                f"Reply with the revised comment only."
                            )
                        }]
                    }]
                )
                new_draft = resp.content[0].text.strip()
                block = block.replace(
                    f"```\n{old_draft}\n```",
                    f"```\n{new_draft}\n```"
                )

        updated_blocks.append(block)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n---\n".join(updated_blocks))

    print(f"\nUpdated {path}")
    print("Review the refined comments, then run: python run.py publish")


def cmd_publish(session = None):
    from report import find_latest_session, parse_approved
    from publish import publish_comments

    d = session or find_latest_session()
    if not d:
        print("No session found.")
        return

    import os
    comments_path = os.path.join(d, "comments.md")
    approved = parse_approved(d)
    if not approved:
        print("No approved comments found. Set STATUS: approved in comments.md")
        return

    print(f"Publishing {len(approved)} approved comments...")
    results = publish_comments(approved, comments_path=comments_path)

    ok = sum(1 for r in results if r["published"])
    print(f"\nDone: {ok}/{len(results)} published successfully.")


def cmd_full():
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts, save_comments
    from knowledge_base import build_context
    from generate_comments import generate_comments
    import json

    posts = fetch_all_posts()
    if not posts:
        print("No posts found.")
        return

    d = session_dir()
    save_posts(posts, d)

    # Save raw JSON for refine/regenerate
    with open(os.path.join(d, "posts.json"), "w") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

    print("Building knowledge base context...")
    kb = build_context()
    print(f"KB context: {len(kb):,} chars")

    _save_to_engagement_db(posts, "linkedin")

    print("Generating comments...")
    items = generate_comments(posts, kb)

    path = save_comments(items, d)
    print(f"\n✓ {len(items)} comment drafts → {path}")
    print(f"\nNext: open {path}")
    print("  Set STATUS: approved / rejected")
    print("  Add FEEDBACK: ... for revisions")
    print("  Then: python run.py refine   or   python run.py publish")


def cmd_fetch_with_json():
    """fetch + save raw JSON for generate step."""
    from fetch_posts import fetch_all_posts
    from report import session_dir, save_posts
    import json

    posts = fetch_all_posts()
    if not posts:
        print("No posts found.")
        return

    d = session_dir()
    save_posts(posts, d)
    with open(os.path.join(d, "posts.json"), "w") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(posts)} posts → {d}")


def cmd_draft_posts():
    from report import find_latest_session
    from knowledge_base import build_context
    from generate_posts import generate_post_drafts, extract_trending_topics
    from datetime import datetime
    import json

    d = find_latest_session()
    if not d:
        print("No session found. Run 'fetch' first.")
        return

    raw_json = os.path.join(d, "posts.json")
    if not os.path.exists(raw_json):
        print("No posts.json found. Run 'fetch' first.")
        return

    with open(raw_json) as f:
        posts = json.load(f)

    print("Extracting trending topics...")
    topics = extract_trending_topics(posts)
    for t in topics[:5]:
        print(f"  {t[:80]}")

    print("\nBuilding knowledge base...")
    kb = build_context()

    print("Generating post drafts...")
    drafts = generate_post_drafts(topics, kb)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    out_path = os.path.join(d, "post_drafts.md")
    lines = [
        f"# Post Drafts — {ts}\n",
        "Instructions: Set STATUS to `approved` or `rejected` for each draft.\n",
        "---\n",
    ]
    for i, draft in enumerate(drafts, 1):
        lines.append(f"## Draft {i}")
        lines.append(f"```\n{draft}\n```\n")
        lines.append("**STATUS:** pending")
        lines.append("**FEEDBACK:**\n")
        lines.append("---\n")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\nSaved {len(drafts)} post drafts → {out_path}")


def _save_to_engagement_db(posts, platform):
    """Append high-engagement posts (100+ likes) to engagement_db.json for pattern analysis."""
    from config import DATA_DIR
    db_path = os.path.join(DATA_DIR, "engagement_db.json")
    if os.path.exists(db_path):
        try:
            with open(db_path) as f:
                db = json.load(f)
        except Exception:
            db = []
    else:
        db = []
    existing_urls = {e["url"] for e in db}
    added = 0
    for p in posts:
        likes = p.get("likes", 0)
        if likes >= 100 and p.get("url") and p["url"] not in existing_urls:
            db.append({
                "platform": platform,
                "url": p["url"],
                "author": p.get("author", ""),
                "text": p.get("text", "")[:500],
                "likes": likes,
                "comments": p.get("comments", p.get("replies", 0)),
                "saved_at": __import__("datetime").datetime.utcnow().isoformat(),
            })
            existing_urls.add(p["url"])
            added += 1
    with open(db_path, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    if added:
        print(f"  Saved {added} high-engagement posts to engagement_db.json")


def cmd_tweets():
    from fetch_tweets import fetch_tweets
    from generate_replies import generate_replies
    from knowledge_base import build_context, save_tweet_example
    from report import session_dir
    from datetime import datetime
    import json

    # Save approved replies from previous session to KB
    from report import find_latest_session
    prev = find_latest_session()
    if prev:
        prev_replies = os.path.join(prev, "replies.md")
        if os.path.exists(prev_replies):
            import re as _re
            with open(prev_replies) as f:
                content = f.read()
            for block in content.split("\n---\n"):
                if "**STATUS:** approved" not in block:
                    continue
                tweet_m = _re.search(r"> (.+)", block)
                reply_m = _re.search(r"```\n([\s\S]+?)\n```", block)
                if tweet_m and reply_m:
                    save_tweet_example(tweet_m.group(1).strip(), reply_m.group(1).strip())

    print("Fetching tweets...")
    tweets = fetch_tweets()
    if not tweets:
        print("No new tweets found.")
        return
    print(f"Total tweets fetched: {len(tweets)}")

    d = session_dir()
    with open(os.path.join(d, "tweets.json"), "w") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)

    _save_to_engagement_db(tweets, "twitter")

    print("Building knowledge base...")
    kb = build_context()

    print("Generating replies...")
    items = generate_replies(tweets, kb)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    path = os.path.join(d, "replies.md")
    lines = [
        f"# Twitter Reply Drafts — {ts}\n",
        "Instructions: Set STATUS to `approved` or `rejected`. Copy approved replies and post manually.\n",
        "---\n",
    ]
    for i, item in enumerate(items, 1):
        if item.get("skip"):
            continue
        lines.append(f"## {i}. @{item.get('author_username', item['author'])}")
        lines.append(f"**URL:** {item['url']}")
        lines.append(f"**Engagement:** {item['likes']} likes · {item.get('replies', 0)} replies\n")
        lines.append("**Tweet:**")
        lines.append(f"> {item['text'][:280].strip().replace(chr(10), ' ')}\n")
        lines.append("**Our reply:**")
        lines.append(f"```\n{item['draft']}\n```\n")
        lines.append("**STATUS:** pending")
        lines.append("**FEEDBACK:**\n")
        lines.append("---\n")

    with open(path, "w") as f:
        f.write("\n".join(lines))

    real = sum(1 for i in items if not i.get("skip"))
    print(f"\n✓ {real} reply drafts → {path}")

    real = sum(1 for i in items if not i.get("skip"))
    print(f"\n✓ {real} reply drafts → {path}")


COMMANDS = {
    "fetch": cmd_fetch_with_json,
    "generate": cmd_generate,
    "refine": cmd_refine,
    "publish": cmd_publish,
    "full": cmd_full,
    "draft_posts": cmd_draft_posts,
    "tweets": cmd_tweets,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
