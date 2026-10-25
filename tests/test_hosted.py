from datetime import datetime, timezone
from pathlib import Path
import yaml

from digest_engine.models import Episode
from digest_engine.store import Store
from scripts import hosted


def test_runner_paths_and_deployment_acknowledgements(tmp_path, monkeypatch):
    spec = yaml.safe_load((Path(__file__).parents[1] / 'blogcast.yaml').read_text())
    spec['cover_image'] = ''
    (tmp_path / 'blogcast.yaml').write_text(yaml.safe_dump(spec))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('NTFY_SUBMISSION_TOPIC', 'test-input')
    monkeypatch.setenv('NTFY_NOTIFICATION_TOPIC', 'test-output')
    monkeypatch.setenv('GITHUB_OUTPUT', str(tmp_path / 'step-output'))
    audio = tmp_path / 'runtime/audio/episode.mp3'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'original audio')
    (tmp_path / 'runtime/daily.txt').write_text(datetime.now(timezone.utc).date().isoformat())
    store = Store(tmp_path / 'runtime/state.db')
    ep = Episode(id='one', digest='blogcast', title='An episode', description='', script='', audio_path='/previous-run/episode.mp3')
    store.save_episode(ep)
    store.conn.executescript("CREATE TABLE submissions (status TEXT); INSERT INTO submissions VALUES ('ready'); CREATE TABLE notification_outbox (episode_id TEXT PRIMARY KEY); INSERT INTO notification_outbox VALUES ('one');")
    store.conn.commit()
    monkeypatch.setattr(hosted.inbox, 'process', lambda *a, **k: 0)
    sent = []
    monkeypatch.setattr(hosted.notify, 'announce', lambda s, eps, b: sent.extend(eps) or 1)
    hosted.prepare()
    assert not sent
    assert (tmp_path / 'step-output').read_text() == 'changed=true\n'
    assert store.all_episodes()[0].audio_path == str(audio)
    assert store.conn.execute('SELECT status FROM submissions').fetchone() == ('ready',)
    hosted.finalize()
    assert len(sent) == 1
    assert store.conn.execute('SELECT status FROM submissions').fetchone() == ('done',)
    hosted.prepare()
    assert (tmp_path / 'step-output').read_text().endswith('changed=false\n')
    hosted.finalize()
    assert len(sent) == 1
