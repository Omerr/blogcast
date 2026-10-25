"""Social collector: an account's own public posts on X and LinkedIn, found through web search.

    {type: social, group: "Horizon3.ai", priority: 0, platform: x, handle: Horizon3ai}
    {type: social, group: "Corma", priority: 0, platform: linkedin, handle: cormaai}

Neither site offers a free API, and scraping them with a logged-in account breaks
their terms. Search engines index public posts, so this asks one (DuckDuckGo, via
the `ddgs` package, no key; Bing first, see BACKENDS) for the handle and keeps only results whose URL is a
post by that account:

    x:        x.com/<handle>/status/<id>
    linkedin: linkedin.com/posts/<handle>_<slug>-activity-<id>-<suffix>

Search dates are unreliable and time-limited queries return almost nothing, so the
query is unrestricted and each post is dated from its id instead. Both ids are
snowflakes whose top bits are a millisecond timestamp. `max_age_days` (default 21)
drops older posts.

A person's account (a founder's) posts about everything. `require` is a regex that the
post's title or text must match, e.g. `require: Corma`, so only posts about the company
count. For a person, `handle` is the LinkedIn vanity name (linkedin.com/in/<handle>);
their posts use the same linkedin.com/posts/<handle>_ URLs as a company's.

Coverage is whatever the search engine has indexed: usually the account's recent
posts, never all of them. `handle` is the company's vanity name on LinkedIn
(linkedin.com/company/<handle>) and its @name on X.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from .models import Item

X_EPOCH_MS = 1288834974657          # Twitter's snowflake epoch
_QUERIES = {
    "x": ['site:x.com "{h}"', "x.com/{h}/status", 'site:x.com "{h}" {g}'],
    "linkedin": ['site:linkedin.com/posts "{h}"', "linkedin.com/posts/{h}", 'site:linkedin.com/posts "{h}" {g}'],
}
# Measured 2026-09-25 on these queries: Bing returned most of the account's own posts,
# Yahoo and Yandex some; DuckDuckGo, Brave, Google, Mojeek and Startpage none.
BACKENDS = ("bing", "yahoo", "yandex")
_GENERIC_TITLE = re.compile(r"^(?:post by .*|.*'s post|linkedin|x)$", re.I)


def post_pattern(platform: str, handle: str) -> re.Pattern:
    h = re.escape(handle)
    if platform == "x":
        return re.compile(rf"^https?://(?:www\.)?(?:x|twitter)\.com/{h}/status/(\d+)", re.I)
    if platform == "linkedin":
        return re.compile(rf"^https?://(?:[a-z]{{2}}\.|www\.)?linkedin\.com/posts/{h}_[^/?#]*?-activity-(\d+)", re.I)
    raise ValueError(f"unknown social platform: {platform!r}")


def post_time(platform: str, post_id: str) -> datetime | None:
    """When the post was made, from its id. None when the id is not a plausible snowflake."""
    ms = int(post_id) >> 22
    if platform == "x":
        ms += X_EPOCH_MS
    try:
        t = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return t if datetime(2006, 1, 1, tzinfo=timezone.utc) < t < datetime.now(timezone.utc) + timedelta(days=1) else None


def canonical(platform: str, url: str) -> str:
    """One URL per post: search results vary the host and query string."""
    url = url.split("?")[0].split("#")[0]
    if platform == "x":
        return re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/", "https://x.com/", url, flags=re.I)
    return re.sub(r"^https?://(?:[a-z]{2}\.|www\.)?linkedin\.com/", "https://www.linkedin.com/", url, flags=re.I)


def _title(platform: str, raw: str, handle: str, body: str = "") -> str:
    """Search titles read 'Horizon3.ai on X: "Cyberspace has…' or 'Announcing … | Corma'.
    Some are only "Mate's Post"; the post text is the better title then."""
    t = raw.strip()
    if platform == "x":
        t = re.sub(r'^.*? on X:\s*"?', "", t)
        t = re.sub(r'"?\s*/\s*X\b.*$', "", t).strip('" ').strip()
    else:
        t = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", t)
        t = re.sub(r"\s*\|\s*[^|]{1,60}$", "", t)
    if (not t or _GENERIC_TITLE.match(t) or not re.sub(r"[#@]\w+", "", t).strip()) and body:
        t = re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0][:140].rstrip()
    return t or f"Post by {handle}"


def _search(query: str, max_results: int) -> list[dict]:
    """The first backend in BACKENDS that returns anything."""
    from ddgs import DDGS  # optional dependency: pip install digest-engine[social]

    last = None
    for backend in BACKENDS:
        try:
            with DDGS() as d:
                found = list(d.text(query, max_results=max_results, backend=backend))
        except Exception as exc:          # ddgs raises when a backend has no results
            last = exc
            continue
        if found:
            return found
    if last:
        raise last
    return []


def collect_social(src: dict, digest: str, search=None, now: datetime | None = None,
                   pause: float = 1.5) -> list[Item]:
    platform, handle = src["platform"].lower(), src["handle"].lstrip("@")
    pattern = post_pattern(platform, handle)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=int(src.get("max_age_days", 21)))
    search = search or _search
    require = re.compile(src["require"], re.I) if src.get("require") else None
    found: dict[str, Item] = {}
    for i, q in enumerate(_QUERIES[platform]):
        if i and pause:
            time.sleep(pause)            # DuckDuckGo refuses rapid bursts
        try:
            results = search(q.format(h=handle, g=src.get("group", "")), int(src.get("max_results", 20)))
        except Exception as exc:          # one failed query should not lose the other
            print(f"  ! social {platform}/{handle}: {exc}")
            continue
        for r in results:
            href = r.get("href") or r.get("url") or ""
            m = pattern.match(href)
            if not m:
                continue
            when = post_time(platform, m.group(1))
            if not when or when < cutoff:
                continue
            url = canonical(platform, href)
            text = f"{r.get('title', '')} {r.get('body') or r.get('snippet') or ''}"
            if require and not require.search(text):
                continue
            found.setdefault(url, Item(
                group=src.get("group", handle), priority=int(src.get("priority", 1)),
                title=_title(platform, r.get("title", ""), handle, r.get("body") or r.get("snippet") or ""), url=url, published=when,
                summary=f"via {'X' if platform == 'x' else 'LinkedIn'} @{handle}",
                content=(r.get("body") or r.get("snippet") or "").strip(),
                source_type="social", digest=digest, meta={"platform": platform, "handle": handle},
            ))
    return sorted(found.values(), key=lambda it: it.published, reverse=True)
