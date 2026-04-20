import re
import time
import random
import requests
from config import UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
from knowledge_base import save_example


def _headers() -> dict:
    return {"X-API-KEY": UNIPILE_API_KEY, "Content-Type": "application/json"}


def _get_social_id(activity_id: str):
    url = f"{UNIPILE_DSN}/api/v1/posts/{activity_id}"
    resp = requests.get(url, headers=_headers(), params={"account_id": UNIPILE_ACCOUNT_ID}, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("social_id")
    return None


def _extract_activity_id(post_url: str):
    m = re.search(r"activity[:\-](\d+)", post_url)
    return m.group(1) if m else None


def _post_comment(social_id: str, text: str) -> tuple:
    url = f"{UNIPILE_DSN}/api/v1/posts/{social_id}/comments"
    resp = requests.post(
        url,
        headers=_headers(),
        json={"account_id": UNIPILE_ACCOUNT_ID, "text": text},
        timeout=15,
    )
    if resp.status_code in (200, 201):
        data = resp.json()
        return True, data.get("comment_id", "ok")
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _mark_published(comments_path: str, url: str):
    """Flip STATUS: approved → STATUS: published for the given post URL."""
    with open(comments_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = content.split("\n---\n")
    updated = []
    for block in blocks:
        if f"**URL:** {url}" in block and "**STATUS:** approved" in block:
            block = block.replace("**STATUS:** approved", "**STATUS:** published")
        updated.append(block)

    with open(comments_path, "w", encoding="utf-8") as f:
        f.write("\n---\n".join(updated))


def publish_comments(approved: list[dict], comments_path: str = None) -> list[dict]:
    results = []

    for i, item in enumerate(approved):
        comment_text = item.get("final") or item.get("draft", "")
        if not comment_text:
            continue

        print(f"  Posting {i+1}/{len(approved)}: {item['author'][:40]}")

        activity_id = _extract_activity_id(item["url"])
        if not activity_id:
            results.append({**item, "published": False, "publish_detail": "could not extract activity ID from URL"})
            continue

        social_id = _get_social_id(activity_id)
        if not social_id:
            results.append({**item, "published": False, "publish_detail": "could not fetch post from Unipile"})
            continue

        ok, detail = _post_comment(social_id, comment_text)
        results.append({**item, "published": ok, "publish_detail": detail})

        if ok:
            print(f"    Posted (comment_id: {detail})")
            save_example(item.get("text", item.get("url", "")), comment_text)
            if comments_path:
                _mark_published(comments_path, item["url"])
        else:
            print(f"    Failed: {detail}")

        if i < len(approved) - 1:
            delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
            print(f"  Waiting {delay}s...")
            time.sleep(delay)

    return results
