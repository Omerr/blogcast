"""Enrichment: clean article text, sanitise for speech, de-duplicate."""
from __future__ import annotations

import html
import re

from .sources import fetch

try:
    import trafilatura  # type: ignore

    _HAS_TRAFILATURA = True
except Exception:
    _HAS_TRAFILATURA = False


def _fallback(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def extract(url: str, max_chars: int = 20000) -> str:
    """Clean article text, or "" if we could not fetch it.

    Never return an error string as content: it used to be narrated aloud
    ("could not fetch HTTP Error 403"). Callers treat "" as a skip.
    """
    try:
        html = fetch(url).decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"  ! fetch failed for {url}: {exc}")
        return ""
    if _HAS_TRAFILATURA:
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True)
        if text:
            return text[:max_chars]
    return _fallback(html)[:max_chars]


def extract_article(url: str, *, title: str = "") -> tuple[str, str]:
    """Extract a submitted HTML article in full, without the digest length cap."""
    data = fetch(url)
    if data.startswith(b"%PDF"):
        raise ValueError("PDF submissions are not supported; send the HTML article URL.")
    raw = data.decode("utf-8", errors="replace")
    if not _HAS_TRAFILATURA:
        raise RuntimeError("Article submission requires trafilatura: install digest-engine[extract].")
    document = trafilatura.bare_extraction(
        raw, url=url, include_comments=False, include_tables=True,
        favor_precision=True, with_metadata=True,
    )
    if not document:
        return title or url, ""
    # Trafilatura 1.x returned dictionaries; 2.x returns Document objects.
    get = document.get if isinstance(document, dict) else lambda key: getattr(document, key, None)
    return title or get("title") or url, get("text") or ""


def clean_for_speech(text: str) -> str:
    """Strip things that sound terrible when read aloud."""
    text = html.unescape(text)                               # &#8230; before # is stripped
    text = re.sub(r"https?://\S+", "", text)                 # URLs
    text = re.sub(r"\[[0-9,\s]+\]", "", text)                # [1], [2,3]
    text = re.sub(r"\([^)]*https?://[^)]*\)", "", text)      # (see link)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)         # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)     # markdown links -> text
    text = re.sub(r"[*_`#>]+", "", text)                     # md symbols
    text = re.sub(r"(?i)\b(read more|share this|subscribe now|advertisement)\b.*?$", "", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedupe(items: list) -> list:
    """Drop items with the same (group, normalized title) within a run."""
    seen: set[tuple[str, str]] = set()
    out = []
    for it in items:
        key = (it.group, normalize_title(it.title))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
