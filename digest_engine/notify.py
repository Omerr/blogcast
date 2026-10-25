"""Push notifications for freshly published episodes.

Currently one provider: [ntfy](https://ntfy.sh) — no account, no key, just a
topic name. Spec:

    notify:
      ntfy:
        topic: my-unguessable-topic
        server: https://ntfy.sh     # or your self-hosted instance
        priority: 3                 # 1 min .. 5 max
        max_individual: 3           # above this, send one summary instead

A topic on the public ntfy.sh is readable and writable by anyone who knows its
name, so treat the name as the secret and keep the payload non-sensitive.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .models import Episode
from .publish import aware as publish_aware

DEFAULT_SERVER = "https://ntfy.sh"
DEFAULT_PRIORITY = 3
DEFAULT_MAX_INDIVIDUAL = 3


def _runtime(duration: str) -> str:
    """"00:07:04" -> "7 min". Short posts make sub-minute episodes, so do not
    round them down to "0 min"."""
    try:
        hours, minutes, seconds = (int(p) for p in duration.split(":"))
    except ValueError:
        return duration
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes} min"
    return f"{seconds} sec"


def _subtitle(ep: Episode) -> str:
    """Runtime and publication date — the two things worth knowing at a glance."""
    return f"{_runtime(ep.duration)} · {publish_aware(ep.published):%b %-d}"


def _post(server: str, payload: dict, timeout: int = 15) -> None:
    req = urllib.request.Request(
        server.rstrip("/") + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def ntfy(spec: dict, episodes: list[Episode], base: str) -> int:
    """Announce `episodes`. Returns how many notifications went out.

    Never raises: a digest that was published successfully should not be
    reported as failed because a notification did not land.
    """
    cfg = ((spec.get("notify") or {}).get("ntfy") or {})
    topic = cfg.get("topic", "")
    if not topic or not episodes:
        return 0

    server = cfg.get("server", DEFAULT_SERVER)
    priority = int(cfg.get("priority", DEFAULT_PRIORITY))
    max_individual = int(cfg.get("max_individual", DEFAULT_MAX_INDIVIDUAL))
    show = f"{base}/"

    if len(episodes) > max_individual:
        titles = "\n".join(f"• {ep.title}" for ep in episodes[:8])
        extra = f"\n… and {len(episodes) - 8} more" if len(episodes) > 8 else ""
        messages = [{
            "topic": topic, "priority": priority, "tags": ["headphones"],
            "title": f"{len(episodes)} new episodes",
            "message": f"{titles}{extra}",
            "click": show,
        }]
    else:
        messages = [{
            "topic": topic, "priority": priority, "tags": ["headphones"],
            "title": ep.title,
            "message": _subtitle(ep),
            "click": show,
        } for ep in episodes]

    sent = 0
    for payload in messages:
        try:
            _post(server, payload)
            sent += 1
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"  ! ntfy notification failed: {exc}")
    if sent:
        print(f"  🔔 notified {topic} ({sent} message(s))")
    return sent


def announce(spec: dict, episodes: list[Episode], base: str) -> int:
    """Fan out to whichever notifiers the spec configures."""
    return ntfy(spec, episodes, base)
