"""LLM operations, provider-agnostic via LiteLLM.

Model strings look like:
    openrouter/deepseek/deepseek-v4.1-flash
    openai/gpt-4o-mini
    gemini/gemini-2.5-flash
    ollama/qwen3
Credentials come from env vars (OPENROUTER_API_KEY, OPENAI_API_KEY, ...).
"""
from __future__ import annotations

import json
import re

from .models import Item


def complete(model: str, system: str, user: str, temperature: float = 0.3) -> str:
    import litellm  # lazy: keeps import light and avoids config surprises

    resp = litellm.completion(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
    )
    return resp.choices[0].message.content or ""


def extract_json(text: str):
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"No JSON found in model output:\n{text[:500]}")


def _catalogue(items: list[Item], offset: int = 0) -> list[dict]:
    """title + summary only. `content` is ~80% of the payload and the scorer
    barely needs it; including it caused context overflow (review §2.8)."""
    return [{
        "id": offset + i, "group": it.group, "priority": it.priority, "title": it.title,
        "summary": (it.summary or "")[:300], "url": it.url,
    } for i, it in enumerate(items)]


SCORE_BATCH = 40


def score_items(items: list[Item], persona: str, model: str, guidance: str = "") -> list[tuple[Item, float, str]]:
    """Rate each item's importance. Batched so 300+ items never overflow."""
    if not items:
        return []
    out: list[tuple[Item, float, str]] = []
    for start in range(0, len(items), SCORE_BATCH):
        batch = items[start:start + SCORE_BATCH]
        prompt = f"""You are an editor. Rate each item for how much it matters to the
subject described in the persona, 0-100, with a <=15-word reason. Lower
`priority` number = more important source. Reward real news; penalise SEO
listicles and recycled PR.

{guidance}

<persona>
{persona}
</persona>

Items:
{json.dumps(_catalogue(batch, start), ensure_ascii=False, indent=1)}

Return ONLY a JSON array: [{{"id":0,"score":87,"reason":"..."}}]"""
        ratings = extract_json(complete(model, "You output only JSON.", prompt, 0.2))
        for r in ratings:
            idx = int(r["id"])
            if 0 <= idx < len(items):
                out.append((items[idx], float(r.get("score", 0)), r.get("reason", "")))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def write_summary(
    items: list[Item], persona: str, kind: str, model: str, guidance: str = ""
) -> tuple[str, str, str]:
    """Summarise items into a spoken script. Returns (title, description, script)."""
    length = {
        "urgent": "A single-item alert, 60-120 seconds. Lead with what changed.",
        "digest": "A single-item briefing, 3-6 minutes.",
        "weekly": "A roundup, 5-10 minutes, most important first.",
        "intro": "A briefing, 3-5 minutes.",
    }.get(kind, "5-10 minutes.")

    payload = [{
        "group": it.group, "title": it.title, "url": it.url,
        "content": (it.content or it.summary or "")[:6000],
    } for it in items]

    prompt = f"""You are the narrator of an audio digest. Write it to be LISTENED to.

{guidance}

<persona>
{persona}
</persona>

Rules: spoken, concise, no filler. Never read URLs or markdown aloud. Do not
invent facts. Return ONLY JSON:
{{"title": "...", "description": "one sentence", "script": "plain narration"}}

Length: {length}
Items:
{json.dumps(payload, ensure_ascii=False, indent=1)}"""
    data = extract_json(complete(model, "You output only JSON.", prompt, 0.4))
    if isinstance(data, list):
        data = data[0]
    return data["title"].strip(), data.get("description", "").strip(), data["script"].strip()