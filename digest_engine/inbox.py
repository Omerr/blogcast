"""Durable local queue for article URLs submitted through ntfy."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from . import resource, site, notify
from .config import base_url, load_spec, resolve
from .store import Store


def receive(cfg: dict) -> list[dict]:
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    topic = urllib.parse.quote(cfg["topic"], safe="")
    # Replay the server cache; message IDs make ingestion idempotent.
    with urllib.request.urlopen(f"{server}/{topic}/json?poll=1&since=all", timeout=30) as response:
        return [json.loads(line) for line in response if line.strip()]


def process(spec_path: str, *, publish_site: bool = True) -> int:
    spec = load_spec(spec_path)
    store = Store(resolve(spec, spec["paths"]["db_file"]))
    conn = store.conn
    conn.execute("""CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')""")
    failed = False
    try:
        try:
            for message in receive(spec["submissions"]):
                if message.get("event") != "message":
                    continue
                conn.execute("INSERT OR IGNORE INTO submissions (id,url) VALUES (?,?)",
                             (message["id"], message.get("message", "").strip()))
            conn.commit()  # Persist before any network-heavy narration.
        except Exception as exc:
            print(f"Could not poll inbox: {exc}")
            failed = True
        pending = conn.execute("SELECT id,url,attempts FROM submissions WHERE status='pending'").fetchall()
        for mid, url, attempts in pending:
            try:
                if any(c.isspace() for c in url):
                    raise ValueError("Send one article URL as the entire message body")
                ep = resource.add(spec_path, url)
                if publish_site:
                    site.publish(spec_path)
            except Exception as exc:
                # Retry transient failures; bound repeated downloads of bad URLs.
                status = "failed" if attempts >= 4 else "pending"
                conn.execute("UPDATE submissions SET attempts=attempts+1,error=?,status=? WHERE id=?",
                             (str(exc), status, mid))
                conn.commit()
                print(f"Submission {mid}: {status}: {exc}")
                if status == "failed":
                    cfg = (spec.get("notify") or {}).get("ntfy") or {}
                    if cfg.get("topic"):
                        try:
                            notify._post(cfg.get("server", "https://ntfy.sh"), {
                                "topic": cfg["topic"], "title": "Blogcast could not add an article",
                                "message": f"{url}\nFailed after five attempts. Send the URL again to retry.",
                                "tags": ["warning"],
                            })
                        except Exception as notification_error:
                            print(f"Failure notification failed: {notification_error}")
                failed = True
                continue
            status = 'done' if publish_site else 'ready'
            conn.execute("UPDATE submissions SET status=?,error='' WHERE id=?", (status, mid))
            if not publish_site:
                conn.execute("CREATE TABLE IF NOT EXISTS notification_outbox (episode_id TEXT PRIMARY KEY)")
                conn.execute("INSERT OR IGNORE INTO notification_outbox VALUES (?)", (ep.id,))
            conn.commit()
            if not publish_site:
                continue
            print(f"Published: {ep.title}")
            try:
                notify.announce(spec, [ep], base_url(spec))
            except Exception as exc:
                print(f"Episode published, but notification failed: {exc}")
        return 1 if failed else 0
    finally:
        conn.close()
