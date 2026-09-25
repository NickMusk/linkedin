import re
import time
import random
import requests
from config import UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX
from knowledge_base import save_example
from fetch_posts import mark_url_published

_DEFAULT_ACCOUNT_ID = UNIPILE_ACCOUNT_ID


def _headers() -> dict:
    return {"X-API-KEY": UNIPILE_API_KEY, "Content-Type": "application/json"}


def _get_social_id(activity_id: str, account_id: str = None):
    account_id = account_id or _DEFAULT_ACCOUNT_ID
    url = f"{UNIPILE_DSN}/api/v1/posts/{activity_id}"
    resp = requests.get(url, headers=_headers(), params={"account_id": account_id}, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("social_id")
    return None


def _extract_activity_id(post_url: str):
    m = re.search(r"activity[:\-](\d+)", post_url)
    return m.group(1) if m else None


# ── Pre-send gate ──────────────────────────────────────────────────────────
# Twice the pipeline published the writer's own self-correction narration
# ("Wait, no emoji. Let me redo: ..."). EVERY outgoing comment must pass this
# gate; on any doubt we drop the comment — losing one comment is cheap,
# posting editor chatter under Nick's name is not.

_GATE_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️]")
_GATE_META_RE = re.compile(
    r"\bwait[,.\s]|let me (redo|try|rewrite|fix)|no emoji|redo:|my (bad|mistake)"
    r"|here['’]s (a|the) (version|comment|take)|revised (version|comment)"
    r"|as an ai|i (can|should)['’]?\w* (not|n['’]t)? ?(help|do that)"
    r"|rewrit|meta.?comment",
    re.IGNORECASE,
)


def _quoted_fragment_repeats(text: str) -> bool:
    """The signature of a leaked redo: the same quoted draft appears twice."""
    frags = re.findall(r'[\"“]([^\"”]{6,})[\"”]', text)
    return len(frags) != len({f.strip().lower() for f in frags})


def _llm_says_clean(text: str) -> bool:
    """Final judgment call by a model: is this exactly one publishable comment?
    Fails CLOSED — if the check can't run, the comment does not go out."""
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY
        resp = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            system=(
                "Does the text contain leaked drafting process: self-correction narration "
                "(like 'Wait, no emoji, let me redo'), two versions of the same phrase, "
                "commentary about writing rules, or an AI refusal? "
                "Reply LEAKED if yes. Reply CLEAN if it is just a normal comment of any "
                "style or length (slang, 'lol', quotes, lists and emoticons are normal). "
                "One word only: CLEAN or LEAKED."
            ),
            messages=[{"role": "user", "content": text}],
        )
        return resp.content[0].text.strip().upper().startswith("CLEAN")
    except Exception as e:
        print(f"  [pre-send gate] LLM check failed ({e}) — блокирую комментарий")
        return False


def comment_is_clean(text: str) -> tuple:
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if _GATE_EMOJI_RE.search(t):
        return False, "emoji (Nick never uses emoji)"
    if _GATE_META_RE.search(t):
        return False, "meta/self-correction phrasing"
    if _quoted_fragment_repeats(t):
        return False, "same quoted fragment twice (draft+redo leak)"
    if not _llm_says_clean(t):
        return False, "LLM gate said not a single clean comment"
    return True, "ok"


def _post_comment(social_id: str, text: str, account_id: str = None) -> tuple:
    clean, reason = comment_is_clean(text)
    if not clean:
        print(f"  [pre-send gate] BLOCKED: {reason} | {text[:120]!r}")
        return False, f"blocked by pre-send gate: {reason}"
    account_id = account_id or _DEFAULT_ACCOUNT_ID
    url = f"{UNIPILE_DSN}/api/v1/posts/{social_id}/comments"
    resp = requests.post(
        url,
        headers=_headers(),
        json={"account_id": account_id, "text": text},
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


def publish_comments(approved: list[dict], comments_path: str = None, account_id: str = None) -> list[dict]:
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

        social_id = _get_social_id(activity_id, account_id=account_id)
        if not social_id:
            results.append({**item, "published": False, "publish_detail": "could not fetch post from Unipile"})
            continue

        ok, detail = _post_comment(social_id, comment_text, account_id=account_id)
        results.append({**item, "published": ok, "publish_detail": detail})

        if ok:
            print(f"    Posted (comment_id: {detail})")
            save_example(item.get("text", item.get("url", "")), comment_text)
            mark_url_published(item["url"])
            if comments_path:
                _mark_published(comments_path, item["url"])
        else:
            print(f"    Failed: {detail}")

        if i < len(approved) - 1:
            delay = random.randint(PUBLISH_DELAY_MIN, PUBLISH_DELAY_MAX)
            print(f"  Waiting {delay}s...")
            time.sleep(delay)

    return results
