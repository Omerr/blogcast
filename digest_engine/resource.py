"""Turn a deliberately submitted article URL into one full-read episode."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from . import digest, enrich
from .config import load_spec, resolve
from .models import Item
from .store import Store


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Enter a public http:// or https:// article URL.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def add(spec_path: str, url: str, *, title: str = ""):
    """Save one episode; failures remain retryable and existing episodes are reused.

    Submission time controls feed order, regardless of the article's age.
    No source collection, age filtering, scoring, or notifications happen here.
    """
    url = normalize_url(url)
    spec = load_spec(spec_path)
    spec["format"] = {**spec["format"], "mode": "full", "per_item": True}
    store = Store(resolve(spec, spec["paths"]["db_file"]))
    try:
        from . import media
        video = media.youtube_url(url)
        if video:
            return media.add(video, spec, store, title=title)
        item = Item(group=urlsplit(url).hostname, priority=0, title=title or url,
                    url=url, source_type="submitted", digest=spec["name"])
        eid = digest._episode_id(spec["name"], item)
        existing = next((ep for ep in store.all_episodes(spec["name"]) if ep.id == eid), None)
        if existing:
            print(f"  already narrated: {existing.title}")
            return existing
        item.title, item.content = enrich.extract_article(url, title=title)
        if not item.content:
            raise ValueError("No readable article text found. Check that the URL is publicly accessible.")
        ep = digest._make_episode([item], spec, "", "", "full", store, episode_id=eid)
        store.mark_seen(item.uid, spec["name"], item.group, item.title, item.url)
        digest._write_feed(spec, store)
        return ep
    finally:
        store.conn.close()
