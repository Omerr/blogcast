"""Shared data structures for digest-engine.

Topic-neutral on purpose. The engine does not know what a "claim" or an
"evidence grade" is — a domain layer can attach arbitrary annotations to an
episode, and the engine will persist and log them without interpreting them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Item:
    """A discovered thing (feed entry, page, news mention)."""

    group: str                # a topic, a feed name, or a competitor
    priority: int             # 0 = most important
    title: str
    url: str
    published: Optional[datetime] = None
    summary: str = ""
    content: str = ""         # extracted body text (for full-article reads)
    source_type: str = ""
    digest: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.group}|{self.url}"


@dataclass
class Episode:
    id: str
    digest: str
    title: str
    description: str
    script: str
    audio_path: str
    published: datetime = field(default_factory=utcnow)
    duration: str = "00:00:00"
    kind: str = "digest"      # digest | full | urgent | weekly | intro
    number: int = 0
    # Arbitrary, domain-owned metadata (e.g. evidence-graded claims). The
    # engine stores and logs it but never interprets it.
    annotations: dict = field(default_factory=dict)
    feedback_url: str = ""


@dataclass
class Feedback:
    """One listener's reaction to one episode."""

    episode_id: str
    rating: int = 0                    # 1..5, 0 = unset
    accuracy: str = ""                 # "accurate" | "issues" | ""
    tags: list[str] = field(default_factory=list)   # e.g. ["obvious", "too-long"]
    note: str = ""
    created: datetime = field(default_factory=utcnow)