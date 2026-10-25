"""Source collectors: rss · sitemap · news · webpage · social.

Each source in a digest spec looks like:
    {type: rss, group: "Horizon3", priority: 0, url: "https://.../feed/"}
    {type: sitemap, group: "Corma", priority: 0, url: ".../sitemap.xml", include: /blog/}
    {type: news, group: "Armadin", priority: 0, query: "Armadin Mandia"}
    {type: webpage, group: "X", priority: 1, url: "...", selector: ""}
    {type: social, group: "Corma", priority: 0, platform: linkedin, handle: cormaai}  (see social.py)

Any source also accepts `include` / `exclude` regexes, matched against each
discovered URL.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from xml.etree import ElementTree

import feedparser

from .arak import collect_arak_docs, collect_arak_page
from .models import Item
from .social import collect_social

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None


def collect_rss(src: dict, digest: str) -> list[Item]:
    parsed = feedparser.parse(fetch(src["url"]))
    out = []
    for e in parsed.entries:
        if not e.get("link"):
            continue
        out.append(Item(
            group=src.get("group", src["url"]), priority=int(src.get("priority", 1)),
            title=(e.get("title") or "").strip(), url=e["link"],
            published=_parse_date(e.get("published") or e.get("updated")),
            summary=e.get("summary", ""), source_type="rss", digest=digest,
        ))
    return out


def _sitemap_entries(root) -> list[tuple[str, str | None]]:
    """(loc, lastmod) pairs from a <urlset>, or a <sitemapindex>'s children."""
    out: list[tuple[str, str | None]] = []
    for el in root:
        local = el.tag.split("}")[-1]
        if local not in ("url", "sitemap"):
            continue
        loc = next((c for c in el if c.tag.split("}")[-1] == "loc"), None)
        if loc is None or not loc.text:
            continue
        lm = next((c for c in el if c.tag.split("}")[-1] == "lastmod"), None)
        out.append((loc.text.strip(), lm.text.strip() if lm is not None and lm.text else None))
    return out


def _walk_sitemap(url: str, depth: int = 0, seen: set | None = None) -> list[tuple[str, str | None]]:
    """Resolve a sitemap OR sitemap index into (page_url, lastmod) pairs.

    Most sites serve a <sitemapindex> at /sitemap.xml; the old code only
    understood <urlset>, so those sources returned a silent zero.
    """
    seen = seen if seen is not None else set()
    if url in seen or depth > 3:
        return []
    seen.add(url)
    root = ElementTree.fromstring(fetch(url))
    if root.tag.split("}")[-1] == "sitemapindex":
        out: list[tuple[str, str | None]] = []
        for loc, _ in _sitemap_entries(root):
            out.extend(_walk_sitemap(loc, depth + 1, seen))
        return out
    return _sitemap_entries(root)


def collect_sitemap(src: dict, digest: str) -> list[Item]:
    include = src.get("include", "")
    out = []
    for url, lastmod in _walk_sitemap(src["url"]):
        if include and include not in url:
            continue
        if include and url.rstrip("/").endswith(include.rstrip("/")):
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        title = re.sub(r"[-_]+", " ", slug).strip().capitalize()
        out.append(Item(
            group=src.get("group", src["url"]), priority=int(src.get("priority", 1)),
            title=title, url=url, published=_parse_date(lastmod),
            source_type="sitemap", digest=digest,
        ))
    return out


def collect_news(src: dict, digest: str) -> list[Item]:
    q = urllib.parse.quote_plus(src["query"])
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    parsed = feedparser.parse(fetch(url))
    out = []
    for e in parsed.entries:
        if not e.get("link"):
            continue
        publisher = (e.get("source") or {}).get("title", "news") if isinstance(e.get("source"), dict) else "news"
        out.append(Item(
            group=src.get("group", src["query"]), priority=int(src.get("priority", 1)),
            title=(e.get("title") or "").strip(), url=e["link"],
            published=_parse_date(e.get("published")), summary=f"via {publisher}",
            source_type="news", digest=digest,
        ))
    return out


def collect_webpage(src: dict, digest: str, max_chars: int = 12000) -> list[Item]:
    raw = fetch(src["url"]).decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:max_chars]
    return [Item(
        group=src.get("group", src["url"]), priority=int(src.get("priority", 1)),
        title=src.get("title", src["url"]), url=src["url"], published=datetime.now(timezone.utc),
        content=text, source_type="webpage", digest=digest,
    )]


def _url_ok(url: str, src: dict) -> bool:
    """Optional per-source `include` / `exclude` regexes, matched against the URL.

    Feeds are not always what they claim: decentespresso.com's "en" RSS carries
    every translation of every post, so a spec needs a way to say which URLs count.
    """
    include, exclude = src.get("include", ""), src.get("exclude", "")
    if include and not re.search(include, url):
        return False
    if exclude and re.search(exclude, url):
        return False
    return True


COLLECTORS = {
    "rss": collect_rss,
    "sitemap": collect_sitemap,
    "news": collect_news,
    "webpage": collect_webpage,
    "arak_docs": collect_arak_docs,
    "arak_page": collect_arak_page,
    "social": collect_social,
}


_HEALTH: dict[str, dict] = {}


def _health_key(src: dict) -> str:
    where = src.get("url") or src.get("query") or src.get("path")
    if not where and src.get("handle"):
        where = f"{src.get('platform')}:{src['handle']}"
    return f"{src.get('group', '?')}|{where or '?'}"


def health_snapshot() -> dict:
    return {k: dict(v) for k, v in _HEALTH.items()}


def collect(sources: Iterable[dict], digest: str) -> list[Item]:
    out: list[Item] = []
    for src in sources:
        kind = src.get("type", "rss")
        fn = COLLECTORS.get(kind)
        key = _health_key(src)
        rec = _HEALTH.setdefault(key, {"type": kind, "group": src.get("group"),
                                       "last_count": 0, "error": None, "runs_zero": 0})
        if not fn:
            print(f"  ! unknown source type: {kind}")
            rec["error"] = f"unknown source type: {kind}"
            continue
        try:
            found = [i for i in fn(src, digest) if _url_ok(i.url, src)]
            out.extend(found)
            rec["last_count"], rec["error"] = len(found), None
        except Exception as exc:  # one bad source shouldn't kill the run
            rec["last_count"], rec["error"] = 0, str(exc)
            print(f"  ! {src.get('group', '?')}/{kind}: {exc}")
    return out