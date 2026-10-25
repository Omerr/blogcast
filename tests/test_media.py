from pathlib import Path
import sys
import types

from digest_engine import media, resource, digest, enrich
from test_submissions import spec_path


def test_youtube_uses_only_original_audio(spec_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('YouTube must never extract article text, compose narration, or synthesize speech')
    monkeypatch.setattr(enrich, 'extract_article', forbidden)
    monkeypatch.setattr(digest, '_make_episode', forbidden)
    monkeypatch.setattr(digest.tts, 'synthesize', forbidden)
    calls = []
    class Downloader:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            calls.append(url)
            Path(self.options['outtmpl'].replace('%(ext)s', 'mp3')).write_bytes(b'original audio')
            return {'title': 'Original title', 'channel': 'Creator', 'duration': 1351}
    monkeypatch.setitem(sys.modules, 'yt_dlp', types.SimpleNamespace(YoutubeDL=Downloader))
    ep = resource.add(spec_path, 'https://youtu.be/fF3lkTCM5-c?t=20')
    again = resource.add(spec_path, 'https://www.youtube.com/watch?v=fF3lkTCM5-c')
    assert again.id == ep.id and len(calls) == 1
    assert ep.script == ''
    assert ep.annotations['mode'] == 'original_audio'
    assert Path(ep.audio_path).read_bytes() == b'original audio'
    assert ep.duration == '00:22:31'


def test_deferred_inbox_waits_for_deployment(spec_path, monkeypatch):
    from digest_engine import inbox
    from digest_engine.models import Episode
    from digest_engine.store import Store
    ep = Episode(id='x', digest='test', title='x', description='', script='', audio_path='')
    monkeypatch.setattr(inbox, 'receive', lambda cfg: [{'id':'one','event':'message','message':'https://example.com/article'}])
    monkeypatch.setattr(resource, 'add', lambda *a: ep)
    monkeypatch.setattr(inbox.site, 'publish', lambda *a: (_ for _ in ()).throw(AssertionError('deferred')))
    monkeypatch.setattr(inbox.notify, 'announce', lambda *a: (_ for _ in ()).throw(AssertionError('deferred')))
    assert inbox.process(spec_path, publish_site=False) == 0
    store = Store(Path(spec_path).parent / 'output/state.db')
    assert store.conn.execute('SELECT status FROM submissions').fetchone() == ('ready',)
    assert store.conn.execute('SELECT episode_id FROM notification_outbox').fetchone() == ('x',)
