"""Compose an episode script from selected items.

Two built-in modes (spec `format.mode`):
  summary  - LLM summarises the items into a spoken briefing
  full     - read the extracted article text aloud, verbatim-ish (no LLM)

A domain layer can supply its own composer via `hooks.compose` in the spec,
e.g. competitor-intel's research + evidence-graded writer.
"""
from __future__ import annotations

import html
import importlib
import re

from . import llm
from .enrich import clean_for_speech
from .models import Item


def load_hook(path: str):
    """Load 'package.module:function' or 'package.module.function'."""
    if ":" in path:
        mod_name, fn_name = path.split(":", 1)
    else:
        mod_name, fn_name = path.rsplit(".", 1)
    module = importlib.import_module(mod_name)
    return getattr(module, fn_name)


def compose(
    items: list[Item], spec: dict, persona: str, guidance: str, kind: str
) -> tuple[str, str, str, dict]:
    """Return (title, description, script, annotations)."""
    hooks = spec.get("hooks", {}) or {}
    if hooks.get("compose"):
        fn = load_hook(hooks["compose"])
        return fn(items, spec, persona, guidance, kind)

    mode = (spec.get("format", {}) or {}).get("mode", "summary")
    if mode == "full":
        return _full_read(items, spec)
    return _summary(items, persona, guidance, kind, spec)


def _drop(text: str, patterns: list[str]) -> str:
    """Remove site cruft (bylines, 'Older:' nav, share prompts) before speaking."""
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.I | re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _min_words(spec: dict) -> int:
    return int((spec.get("format", {}) or {}).get("min_words", 0))


def _full_read(items: list[Item], spec: dict) -> tuple[str, str, str, dict]:
    fmt = spec.get("format", {}) or {}
    patterns = fmt.get("drop_patterns", []) or []
    prefix_group = fmt.get("title_prefix_group", False)
    min_words = _min_words(spec)

    parts: list[str] = []
    used: list[Item] = []
    for it in items:
        text = _drop(clean_for_speech(it.content or it.summary or ""), patterns)
        if len(text.split()) < min_words:
            continue
        head = it.title if it.title.rstrip().endswith((".", "?", "!")) else f"{it.title}."
        if it.published:
            head += f" From {it.group}, {it.published:%B %-d, %Y}."
        else:
            head += f" From {it.group}."
        parts.append(f"{head}\n\n{text}")
        used.append(it)

    if not parts:
        raise ValueError("full mode: no article text available (too short, or extraction empty?)")

    if len(used) == 1:
        it = used[0]
        title = f"{it.group} · {it.title}" if prefix_group else it.title
        description = _description(it)
    else:
        title = f"Full read — {len(used)} items"
        description = "Full read: " + "; ".join(i.title for i in used[:3])

    annotations = {"mode": "full", "sources": [i.url for i in used]}
    return title, description, "\n\n".join(parts), annotations


def _description(it: Item) -> str:
    blurb = clean_for_speech(html.unescape(re.sub(r"(?s)<[^>]+>", " ", it.summary or "")))
    blurb = re.sub(r"\s+", " ", blurb).strip()
    if len(blurb) > 400:
        blurb = blurb[:400].rsplit(" ", 1)[0] + "…"
    stamp = f" ({it.published:%Y-%m-%d})" if it.published else ""
    lead = f"{it.group}{stamp}"
    return f"{lead}\n\n{blurb}\n\nOriginal: {it.url}".strip() if blurb else f"{lead}\n\nOriginal: {it.url}"


def _summary(items, persona, guidance, kind, spec) -> tuple[str, str, str, dict]:
    model = spec["curation"]["model"]
    title, description, script = llm.write_summary(items, persona, kind, model, guidance)
    return title, description, script, {"mode": "summary"}
