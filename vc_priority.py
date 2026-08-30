"""VC priority list shared by the Render pipeline and local browser scripts.

Loads X handles of VC people from the Google Sheet (same sheet the follow
campaign uses), caches them locally so a sheet outage never breaks the
pipeline. Used to (a) fetch tweets directly from these people and (b) mark
queue items as VC-priority so they get replied to first.
"""

import csv
import io
import json
import logging
import os
import re
import time
import urllib.request

log = logging.getLogger(__name__)

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "17lW6qSMOKPJK--Vr-ZSyCEzxHmsW_O4iQ8k9_3cFxCw/export?format=csv"
)
CACHE_FILE = os.path.join(os.path.dirname(__file__), "vc_handles_cache.json")
CACHE_TTL = 6 * 3600

_PROFILE_RE = re.compile(r"^https?://x\.com/([A-Za-z0-9_]{1,15})/?$")

# Keeps VC detection alive even if both the sheet and the cache are gone.
_FALLBACK_HANDLES = [
    "saranormous", "ttunguz", "eladgil", "roybahat", "edsim", "jamescham",
    "hunterwalk", "chudson", "lpolovets", "apartovi", "harjtaggar", "immad",
    "gokulr", "scottbelsky", "nbt", "mamoonha", "ashugarg", "btrenchard",
]


_LINKEDIN_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)/?", re.IGNORECASE)


def _fetch_from_sheet() -> dict:
    with urllib.request.urlopen(SHEET_CSV_URL, timeout=20) as resp:
        data = resp.read().decode("utf-8")
    handles, linkedin = [], []
    li_seen = set()
    for row in csv.DictReader(io.StringIO(data)):
        m = _PROFILE_RE.match((row.get("X ссылка") or "").strip())
        if m:
            handles.append(m.group(1).lower())
        lm = _LINKEDIN_RE.search((row.get("LinkedIn") or "").strip())
        if lm and lm.group(1).lower() not in li_seen:
            li_seen.add(lm.group(1).lower())
            linkedin.append({
                "name": (row.get("Партнер") or "").strip(),
                "fund": (row.get("Фонд") or "").strip(),
                "linkedin_url": f"https://www.linkedin.com/in/{lm.group(1)}/",
            })
    return {"handles": sorted(set(handles)), "linkedin": linkedin}


_memo = {"ts": 0.0, "data": None}


def _get_sheet_data() -> dict:
    """{"handles": [...], "linkedin": [...]} from the sheet, cached 6h.

    In-process memo first: is_vc() is called per queue item, and the disk
    cache may be unwritable (read-only container) — never refetch the sheet
    more than once per TTL within one process.
    """
    if _memo["data"] is not None and time.time() - _memo["ts"] < CACHE_TTL:
        return _memo["data"]
    cached = None
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cached = json.load(f)
        except Exception:
            cached = None
    if cached and "linkedin" in cached and time.time() - cached.get("ts", 0) < CACHE_TTL:
        data = {"handles": cached["handles"], "linkedin": cached["linkedin"]}
    else:
        try:
            data = _fetch_from_sheet()
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump({"ts": time.time(), **data}, f)
            except OSError:
                pass
        except Exception as e:
            log.warning(f"vc_priority: sheet fetch failed ({e}), using stale cache/fallback")
            if cached:
                data = {"handles": cached.get("handles", []), "linkedin": cached.get("linkedin", [])}
            else:
                data = {"handles": _FALLBACK_HANDLES, "linkedin": []}
    _memo.update(ts=time.time(), data=data)
    return data


def get_vc_handles() -> set[str]:
    """Lowercased X handles from the sheet."""
    return set(_get_sheet_data()["handles"])


def get_vc_linkedin_profiles() -> list[dict]:
    """[{name, fund, linkedin_url}] for sheet rows with a direct LinkedIn link."""
    return _get_sheet_data()["linkedin"]


def is_vc(username: str) -> bool:
    return bool(username) and username.lower().lstrip("@") in get_vc_handles()


def vc_search_terms(chunk_size: int = 10) -> list[str]:
    """X search queries fetching recent tweets straight from the VC list,
    chunked to stay within X's query length limits."""
    handles = sorted(get_vc_handles())
    return [
        " OR ".join(f"from:{h}" for h in handles[i:i + chunk_size])
        for i in range(0, len(handles), chunk_size)
    ]
