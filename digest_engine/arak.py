"""Arak docsite collector — diff a captured competitor docsite, emit items.

Arak Pages (github.com/nullprovenance/arak-site) hosts captured docsite mirrors
as self-contained HTML with an embedded payload:

    <script id="<name>-payload" type="application/octet-stream">BASE64</script>

where BASE64 decodes to gzip, which decompresses to JSON `{shell, pages,
titles, order}` — `pages` is slug -> rendered HTML, `order` is the nav order.

This collector snapshots the navigation, page set, and the vendor-authored
`whats-new` changelog, diffs against the last snapshot, and emits one `Item`
per change. It is topic-neutral: it knows nothing about any competitor, only
the docsite format.

Reads the repo through `gh` (GitHub API), so it works behind Cloudflare Access.
Requires the `arak-publish` skill's prerequisite: `gh` authenticated with read
access to nullprovenance/arak-site.

Config:
    {type: arak_docs, group: "Terra Security", priority: 0,
     path: "demos/terra-docs", state: "knowledge/.docsnap/terra.json"}
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import Item

REPO = "nullprovenance/arak-site"
PAYLOAD_RE = re.compile(
    r'<script[^>]*id="[^"]*-payload"[^>]*type="application/octet-stream"[^>]*>\s*([A-Za-z0-9+/=\s]+?)\s*</script>',
    re.S,
)


# ---------------------------------------------------------------- decode
def decode_payload(html: str) -> dict:
    """Extract and decode the embedded {shell,pages,titles,order} payload."""
    m = PAYLOAD_RE.search(html)
    if not m:
        raise ValueError("no arak payload found in page")
    raw = re.sub(r"\s+", "", m.group(1))
    return json.loads(gzip.decompress(base64.b64decode(raw)).decode("utf-8"))


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    import html as _html

    return re.sub(r"\s+", " ", _html.unescape(fragment)).strip()


def whats_new_entries(pages: dict, titles: dict) -> list[dict]:
    """Parse the 'whats-new' page into dated, categorised entries."""
    slug = next((s for s in pages if "whats-new" in s.lower() or "changelog" in s.lower()), None)
    if not slug:
        return []
    html = pages[slug]
    entries: list[dict] = []
    month = category = ""
    # walk the document in order so each <li> inherits its h2/h3 context
    for m in re.finditer(r"<(h2|h3|li)[^>]*>(.*?)</\1>", html, re.S | re.I):
        tag, body = m.group(1).lower(), m.group(2)
        if tag == "h2":
            month, category = _text(body), ""
        elif tag == "h3":
            category = _text(body)
        else:
            text = _text(body)
            if not text:
                continue
            eid = hashlib.sha1(f"{month}|{category}|{text}".encode()).hexdigest()[:12]
            entries.append({"id": eid, "month": month, "category": category,
                            "title": text[:90], "text": text})
    return entries


def snapshot_of(payload: dict) -> dict:
    pages, titles, order = payload.get("pages", {}), payload.get("titles", {}), payload.get("order", [])
    return {
        "nav": list(order),
        "titles": {k: titles.get(k, k) for k in order},
        "pages": sorted(pages),
        "page_hashes": {slug: hashlib.sha1(html.encode()).hexdigest()[:12] for slug, html in pages.items()},
        "whats_new": [e["id"] for e in whats_new_entries(pages, titles)],
    }


# ---------------------------------------------------------------- diff
def _parse_month(month: str):
    try:
        return datetime.strptime(month, "%B %Y").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def diff(prev: dict, cur: dict, path: str, group: str, priority: int, digest: str,
         pages: dict | None = None, titles: dict | None = None) -> list[Item]:
    """One Item per navigation addition/removal and per new whats-new entry."""
    out: list[Item] = []
    pages = pages or {}
    titles = titles or {}

    def link(frag: str) -> str:
        return f"https://html.kinesic.dev/{path}#{frag}"

    prev_nav, cur_nav = set(prev.get("nav", [])), set(cur.get("nav", []))
    for slug in cur_nav - prev_nav:
        title = cur["titles"].get(slug, slug)
        out.append(Item(
            group=group, priority=priority, digest=digest,
            title=f"{group} added \u201c{title}\u201d to its documentation",
            url=link(slug), published=datetime.now(timezone.utc),
            content=_text(pages.get(slug, ""))[:4000], source_type="arak_docs",
        ))
    for slug in prev_nav - cur_nav:
        title = prev.get("titles", {}).get(slug, slug)
        out.append(Item(
            group=group, priority=priority, digest=digest,
            title=f"{group} removed \u201c{title}\u201d from its documentation",
            url=link("removed-" + slug), published=datetime.now(timezone.utc),
            source_type="arak_docs",
        ))

    prev_wn, cur_wn = set(prev.get("whats_new", [])), set(cur.get("whats_new", []))
    if pages:
        for e in whats_new_entries(pages, titles):
            if e["id"] in prev_wn or e["id"] in cur_wn - prev_wn:
                if e["id"] in cur_wn - prev_wn:
                    out.append(Item(
                        group=group, priority=priority, digest=digest,
                        title=e["title"], url=link("whats-new-" + e["id"]),
                        published=_parse_month(e["month"]),
                        summary=f"{e['month']} · {e['category']}" if e["category"] else e["month"],
                        content=e["text"], source_type="arak_docs",
                    ))
    return out


# ---------------------------------------------------------------- fetch
def _gh_read(path: str) -> str:
    url = f"repos/{REPO}/contents/uploaded-htmls/{path}.html"
    proc = subprocess.run(
        ["gh", "api", url, "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh api failed")
    return proc.stdout


def collect_arak_page(src: dict, digest: str) -> list[Item]:
    """Emit one Item from a plain Arak page (e.g. expert-call notes).

    Unlike `arak_docs` this is not a differ — expert-call notes are static
    primary sources. The item carries its `provenance` so the domain layer can
    cite it up to the asset kind's ceiling (an expert call is `third_party`).
    """
    path = src["path"]
    html = _gh_read(path)
    text = _text(html)[:20000]
    url = src.get("url") or f"https://html.kinesic.dev/{path}"
    published = None
    if src.get("captured"):
        try:
            published = datetime.fromisoformat(str(src["captured"])).replace(tzinfo=timezone.utc)
        except ValueError:
            published = None
    return [Item(
        group=src.get("group", path), priority=int(src.get("priority", 1)),
        title=src.get("title") or path.split("/")[-1].replace("-", " ").title(),
        url=url, published=published, content=text,
        source_type="arak_page", digest=digest,
        meta={"provenance": src.get("provenance", "expert_call"), "asset_path": path},
    )]


def collect_arak_docs(src: dict, digest: str) -> list[Item]:
    path = src["path"]
    group = src.get("group", path)
    priority = int(src.get("priority", 1))
    state_path = Path(src.get("state", f".arak/{path.replace('/', '_')}.json"))

    html = _gh_read(path)
    payload = decode_payload(html)
    cur = snapshot_of(payload)

    prev = None
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            prev = None

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(cur, indent=2), encoding="utf-8")

    if prev is None:                       # first run establishes the baseline
        print(f"  · arak baseline captured: {path} ({len(cur['nav'])} nav, {len(cur['whats_new'])} changelog entries)")
        return []
    return diff(prev, cur, path, group, priority, digest,
                pages=payload.get("pages", {}), titles=payload.get("titles", {}))