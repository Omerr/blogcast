"""Config + digest-spec loading. Keeps the engine free of repo-specific paths."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env loader (no dependency). Does not override real env vars."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


def load_yaml(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def feedback_url(spec: dict, episode_id: str) -> str:
    """A one-tap feedback link for an episode.

    Prefers a prefilled GitHub issue (no new infra, durable, easy for the team);
    falls back to mailto: if EMAIL_TO is set.
    """
    import urllib.parse

    repo = (spec.get("feedback", {}) or {}).get("issue_repo", "")
    if repo:
        title = urllib.parse.quote(f"Feedback: {episode_id}")
        body = urllib.parse.quote(
            f"Episode: {episode_id}\n\nWhat was wrong, missing, or unclear? Be blunt.\n\n"
        )
        return (f"https://github.com/{repo}/issues/new?labels=episode-feedback"
                f"&title={title}&body={body}")
    email_to = os.environ.get("EMAIL_TO", "")
    return f"mailto:{email_to}?subject=feedback:%20{episode_id}" if email_to else ""


def base_url(spec: dict) -> str:
    """Where the phone fetches feed.xml and audio from.

    BASE_URL wins, then the spec, then the GitHub Pages URL implied by
    `publish.repo` — so a Pages-hosted digest needs no extra config.
    """
    for candidate in (os.environ.get("BASE_URL"), spec.get("base_url")):
        if candidate:
            return candidate.rstrip("/")
    repo = (spec.get("publish", {}) or {}).get("repo", "")
    if repo.count("/") == 1:
        owner, name = repo.split("/")
        return f"https://{owner.lower()}.github.io/{name}"
    return "http://localhost:8000"


DEFAULT_SPEC = {
    "name": "digest",
    "title": "Digest",
    "description": "An audio digest.",
    "language": "en-us",
    "author": "",
    "cover_image": "",
    "persona": "",                       # inline text or path to a .md file
    "sources": [],                       # [{type, group, priority, ...}]
    "curation": {
        "model": "openrouter/deepseek/deepseek-v4.1-flash",
        "urgent_threshold": 80,
        "weekly_threshold": 55,
        "full_length_threshold": 90,
        "max_items_per_weekly": 8,
        "research": True,                # verify claims before writing
    },
    "format": {"kind": "solo", "length": "weekly", "mode": "summary"},
    "tts": {"provider": "edge", "voice": "en-US-AndrewMultilingualNeural", "rate": "+0%",
            "model": "", "words_per_minute": 155, "pronunciations": {}},
    "scrub": {"enabled": True, "llm": False, "aggressive": False, "model": ""},
    "hooks": {},
    "sinks": ["podcast_rss"],
    "ntfy": {"topic": ""},
    "publish": {"repo": "", "branch": "main", "retention": 80, "cname": ""},
    "notify": {},                        # e.g. {ntfy: {topic: ...}}
    "feedback": {"enabled": True, "guidance": "feedback/guidance.md", "seed": "feedback/seed.md"},
    "paths": {"output_dir": "output", "audio_dir": "output/audio",
              "feed_file": "output/feed.xml", "db_file": "output/state.db",
              "runs_dir": "output/runs", "site_dir": "output/site"},
}


def load_spec(path: str | Path) -> dict:
    """Load a digest spec and merge it over the defaults."""
    raw = load_yaml(path) or {}
    spec = dict(DEFAULT_SPEC)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            merged = dict(spec[key])
            merged.update(value)
            spec[key] = merged
        else:
            spec[key] = value

    # resolve persona if it is a path
    persona = spec.get("persona", "")
    if isinstance(persona, str) and persona.endswith((".md", ".txt")):
        persona_path = Path(persona)
        if not persona_path.is_absolute():
            persona_path = Path(path).resolve().parent / persona_path
        spec["persona"] = persona_path.read_text(encoding="utf-8")
    spec["_path"] = str(Path(path).resolve())
    spec["_root"] = str(Path(path).resolve().parent)
    return spec


def resolve(spec: dict, relative: str | Path) -> Path:
    """Resolve a path relative to the spec file's directory."""
    p = Path(relative)
    return p if p.is_absolute() else Path(spec["_root"]) / p