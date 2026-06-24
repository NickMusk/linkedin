"""
Official X (Twitter) API v2 client for posting — OAuth 1.0a user context.

Replaces the old scrape-based CreateTweet (publish_tweets._post_reply), which
triggered X's automation block (error 226) from datacenter IPs. The official
API is the sanctioned path: posting consumes pay-per-use credits (~$0.015/post,
$0.20 if the post contains a link) as of Feb 2026.

No external deps — OAuth 1.0a is signed with the stdlib (hmac/hashlib/base64).
"""
import os
import time
import hmac
import json
import base64
import hashlib
import logging
import urllib.parse
import urllib.request

from config import (
    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET,
)

log = logging.getLogger(__name__)

_API = "https://api.twitter.com/2"


def _pe(s: str) -> str:
    """Percent-encode per OAuth 1.0a (RFC 3986)."""
    return urllib.parse.quote(str(s), safe="~")


def _auth_header(method: str, url: str) -> str:
    oauth = {
        "oauth_consumer_key": X_API_KEY,
        "oauth_token": X_ACCESS_TOKEN,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": base64.b64encode(os.urandom(16)).decode().strip("="),
        "oauth_version": "1.0",
    }
    # JSON-body v2 endpoints: only oauth params (+ any query params) are signed.
    base = "&".join(f"{_pe(k)}={_pe(oauth[k])}" for k in sorted(oauth))
    base_str = "&".join([method.upper(), _pe(url), _pe(base)])
    signing_key = f"{_pe(X_API_SECRET)}&{_pe(X_ACCESS_TOKEN_SECRET)}"
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()
    ).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pe(k)}="{_pe(v)}"' for k, v in oauth.items())


def _request(method: str, path: str, body: dict | None = None) -> tuple[bool, dict | str]:
    url = f"{_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header(method, url))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return True, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, str(e)


def post_tweet(text: str) -> tuple[bool, str]:
    """Publish an original tweet. Returns (ok, tweet_url_or_error)."""
    ok, res = _request("POST", "/tweets", {"text": text})
    if ok and isinstance(res, dict):
        tid = (res.get("data") or {}).get("id", "")
        if tid:
            return True, f"https://x.com/i/status/{tid}"
        return False, f"No tweet id in response: {str(res)[:200]}"
    return False, str(res)


def reply(text: str, in_reply_to_tweet_id: str) -> tuple[bool, str]:
    """Reply to a tweet. Returns (ok, tweet_url_or_error)."""
    body = {"text": text, "reply": {"in_reply_to_tweet_id": str(in_reply_to_tweet_id)}}
    ok, res = _request("POST", "/tweets", body)
    if ok and isinstance(res, dict):
        tid = (res.get("data") or {}).get("id", "")
        if tid:
            return True, f"https://x.com/i/status/{tid}"
        return False, f"No tweet id in response: {str(res)[:200]}"
    return False, str(res)


def delete_tweet(tweet_id: str) -> tuple[bool, str]:
    ok, res = _request("DELETE", f"/tweets/{tweet_id}")
    return ok, str(res)


def verify() -> tuple[bool, str]:
    """Confirm credentials work. Returns (ok, @username or error)."""
    ok, res = _request("GET", "/users/me")
    if ok and isinstance(res, dict):
        return True, "@" + (res.get("data") or {}).get("username", "?")
    return False, str(res)
