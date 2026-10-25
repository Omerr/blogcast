import copy
from pathlib import Path

import pytest
import yaml

from digest_engine import inbox, resource, enrich, digest
from digest_engine.config import DEFAULT_SPEC
from digest_engine.models import Episode
from digest_engine.store import Store


@pytest.fixture
def spec_path(tmp_path):
    spec = copy.deepcopy(DEFAULT_SPEC)
    spec.update(name='test', submissions={'topic': 'test'}, feedback={'enabled': False},
                format={'mode': 'full', 'min_words': 3}, sinks=['podcast_rss'])
    path = tmp_path / 'spec.yaml'
    path.write_text(yaml.safe_dump(spec))
    return str(path)


def test_add_full_article_and_dedupe(spec_path, monkeypatch):
    monkeypatch.setattr(enrich, 'extract_article', lambda *a, **k: ('Old article', 'full body words ' * 3000))
    calls = []
    def synth(script, path, cfg):
        calls.append(script)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
    monkeypatch.setattr(digest.tts, 'synthesize', synth)
    monkeypatch.setattr(digest.tts, 'measure_duration', lambda p: '00:10:00')
    ep = resource.add(spec_path, 'https://example.com/old#section')
    again = resource.add(spec_path, 'https://example.com/old')
    assert again.id == ep.id
    assert len(calls) == 1 and len(calls[0]) > 20000
    assert ep.published.year >= 2026
    assert ep.annotations['sources'] == ['https://example.com/old']


def test_failed_narration_can_retry(spec_path, monkeypatch):
    monkeypatch.setattr(enrich, 'extract_article', lambda *a, **k: ('Article', 'some full article text'))
    monkeypatch.setattr(digest.tts, 'synthesize', lambda *a: (_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(OSError):
        resource.add(spec_path, 'https://example.com/article')
    store = Store(Path(spec_path).parent / 'output/state.db')
    assert not store.all_episodes()
    assert not store.is_seen('example.com|https://example.com/article')


def test_inbox_publish_retry_and_replay(spec_path, monkeypatch):
    message = {'id': 'abc', 'event': 'message', 'message': 'https://example.com/article'}
    monkeypatch.setattr(inbox, 'receive', lambda cfg: [message])
    adds = []
    ep = Episode(id='ep', digest='test', title='Article', description='', script='', audio_path='')
    monkeypatch.setattr(resource, 'add', lambda *a: adds.append(a) or ep)
    monkeypatch.setattr(inbox.site, 'publish', lambda *a: (_ for _ in ()).throw(OSError('push failed')))
    monkeypatch.setattr(inbox.notify, 'announce', lambda *a: True)
    assert inbox.process(spec_path) == 1
    monkeypatch.setattr(inbox.site, 'publish', lambda *a: None)
    assert inbox.process(spec_path) == 0
    assert inbox.process(spec_path) == 0
    assert len(adds) == 2  # No replay after successful publish.


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://user:pass@example.com', 'garbage'])
def test_invalid_url(url):
    with pytest.raises(ValueError):
        resource.normalize_url(url)


def test_queue_survives_poll_outage_and_bounds_retries(spec_path, monkeypatch):
    monkeypatch.setattr(inbox, 'receive', lambda cfg: [{'id': 'bad', 'event': 'message', 'message': 'bad-url'}])
    assert inbox.process(spec_path) == 1
    monkeypatch.setattr(inbox, 'receive', lambda cfg: (_ for _ in ()).throw(OSError('network down')))
    for _ in range(5):
        assert inbox.process(spec_path) == 1
    store = Store(Path(spec_path).parent / 'output/state.db')
    assert store.conn.execute('SELECT status,attempts FROM submissions').fetchone() == ('failed', 5)
