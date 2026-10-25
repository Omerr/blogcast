"""SQLite ledger: seen items, episodes, feedback."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Episode, Feedback

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    uid TEXT PRIMARY KEY, digest TEXT, grp TEXT, title TEXT, url TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, digest TEXT, title TEXT, description TEXT, script TEXT,
    audio_path TEXT, published TEXT, duration TEXT, kind TEXT, number INTEGER,
    annotations TEXT, feedback_url TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT, rating INTEGER, accuracy TEXT, tags TEXT, note TEXT, created TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        import sqlite3

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- seen items --------------------------------------------------------
    def is_seen(self, uid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone() is not None

    def mark_seen(self, uid: str, digest: str, group: str, title: str, url: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (uid, digest, grp, title, url) VALUES (?,?,?,?,?)",
            (uid, digest, group, title, url),
        )
        self.conn.commit()

    # -- episodes ----------------------------------------------------------
    def next_number(self, digest: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(number),0) FROM episodes WHERE digest=?", (digest,)
        ).fetchone()
        return int(row[0]) + 1

    def has_episode(self, episode_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone() is not None

    def save_episode(self, ep: Episode) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO episodes
               (id,digest,title,description,script,audio_path,published,duration,kind,number,annotations,feedback_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ep.id, ep.digest, ep.title, ep.description, ep.script, ep.audio_path,
             ep.published.isoformat(), ep.duration, ep.kind, ep.number,
             json.dumps(ep.annotations, default=str), ep.feedback_url),
        )
        self.conn.commit()

    def all_episodes(self, digest: Optional[str] = None) -> list[Episode]:
        q = "SELECT * FROM episodes"
        args: tuple = ()
        if digest:
            q += " WHERE digest=?"
            args = (digest,)
        q += " ORDER BY number ASC"
        out: list[Episode] = []
        for row in self.conn.execute(q, args).fetchall():
            (eid, dg, title, desc, script, audio, pub, dur, kind, num, annotations, furl) = row
            out.append(Episode(
                id=eid, digest=dg, title=title, description=desc, script=script,
                audio_path=audio, published=datetime.fromisoformat(pub), duration=dur,
                kind=kind, number=num, annotations=json.loads(annotations or "{}"),
                feedback_url=furl or "",
            ))
        return out

    # -- feedback ----------------------------------------------------------
    def save_feedback(self, fb: Feedback) -> None:
        self.conn.execute(
            "INSERT INTO feedback (episode_id, rating, accuracy, tags, note, created) VALUES (?,?,?,?,?,?)",
            (fb.episode_id, fb.rating, fb.accuracy, ",".join(fb.tags), fb.note, fb.created.isoformat()),
        )
        self.conn.commit()

    def all_feedback(self, episode_id: Optional[str] = None) -> list[Feedback]:
        q = "SELECT episode_id, rating, accuracy, tags, note, created FROM feedback"
        args: tuple = ()
        if episode_id:
            q += " WHERE episode_id=?"
            args = (episode_id,)
        out: list[Feedback] = []
        for eid, rating, acc, tags, note, created in self.conn.execute(q, args).fetchall():
            out.append(Feedback(
                episode_id=eid, rating=rating or 0, accuracy=acc or "",
                tags=[t for t in (tags or "").split(",") if t], note=note or "",
                created=datetime.fromisoformat(created),
            ))
        return out