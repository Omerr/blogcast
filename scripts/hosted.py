"""Prepare episodes, then acknowledge requests only after Pages deploys."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from digest_engine import digest, inbox, notify, site
from digest_engine.config import load_spec, resolve, base_url
from digest_engine.store import Store


def config():
    raw = yaml.safe_load(Path('blogcast.yaml').read_text())
    raw['submissions']['topic'] = os.environ['NTFY_SUBMISSION_TOPIC']
    raw['notify']['ntfy']['topic'] = os.environ['NTFY_NOTIFICATION_TOPIC']
    if not raw['submissions']['topic']:
        raise ValueError('NTFY_SUBMISSION_TOPIC is missing')
    Path('runtime-config.yaml').write_text(yaml.safe_dump(raw))
    return load_spec('runtime-config.yaml')


def open_store(spec):
    store = Store(resolve(spec, spec['paths']['db_file']))
    store.conn.execute('CREATE TABLE IF NOT EXISTS notification_outbox (episode_id TEXT PRIMARY KEY)')
    store.conn.commit()
    return store


def prepare():
    spec = config()
    # Each GitHub runner has a new checkout directory. Rebase persisted paths.
    store = open_store(spec)
    for ep in store.all_episodes(spec['name']):
        path = resolve(spec, spec['paths']['audio_dir']) / Path(ep.audio_path).name
        store.conn.execute('UPDATE episodes SET audio_path=? WHERE id=?', (str(path), ep.id))
    store.conn.commit()
    store.conn.close()
    inbox.process('runtime-config.yaml', publish_site=False)
    today = datetime.now(timezone.utc).date().isoformat()
    daily = Path('runtime/daily.txt')
    if not daily.exists() or daily.read_text().strip() != today:
        episodes = digest.run('runtime-config.yaml', weekly=True, limit=5)
        store = open_store(spec)
        store.conn.executemany('INSERT OR IGNORE INTO notification_outbox VALUES (?)', [(ep.id,) for ep in episodes])
        store.conn.commit()
        store.conn.close()
        daily.write_text(today)
    store = open_store(spec)
    site_dir = site.build(spec, store)
    store.conn.close()
    checksum = hashlib.sha256((site_dir / 'feed.xml').read_bytes()).hexdigest()
    previous = Path('runtime/deployed-feed.sha256')
    changed = not previous.exists() or previous.read_text().strip() != checksum
    Path('runtime/prepared-feed.sha256').write_text(checksum)
    with open(os.environ.get('GITHUB_OUTPUT', '/dev/stdout'), 'a') as out:
        out.write(f'changed={str(changed).lower()}\n')


def finalize():
    spec = config()
    store = open_store(spec)
    store.conn.execute("UPDATE submissions SET status='done' WHERE status='ready'")
    store.conn.commit()
    episodes = {ep.id: ep for ep in store.all_episodes(spec['name'])}
    for (eid,) in store.conn.execute('SELECT episode_id FROM notification_outbox').fetchall():
        if eid in episodes and notify.announce(spec, [episodes[eid]], base_url(spec)):
            store.conn.execute('DELETE FROM notification_outbox WHERE episode_id=?', (eid,))
            store.conn.commit()
    store.conn.close()
    Path('runtime/deployed-feed.sha256').write_text(Path('runtime/prepared-feed.sha256').read_text())


if __name__ == '__main__':
    import sys
    {'prepare': prepare, 'finalize': finalize}[sys.argv[1]]()
