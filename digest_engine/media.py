"""Import original audio from a YouTube video, without speech synthesis."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import re

from . import digest, tts
from .config import resolve
from .models import Episode, Item


def youtube_url(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    video = ''
    if host in {'youtu.be', 'www.youtu.be'}:
        video = parts.path.strip('/').split('/')[0]
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        if parts.path == '/watch':
            video = parse_qs(parts.query).get('v', [''])[0]
        elif parts.path.startswith(('/shorts/', '/live/', '/embed/')):
            video = parts.path.split('/')[2]
        else:
            raise ValueError('Send a single YouTube video URL, not a channel or playlist.')
    else:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        raise ValueError('Invalid YouTube video ID')
    return f'https://www.youtube.com/watch?v={video}'


def add(url: str, spec: dict, store, *, title: str = '') -> Episode:
    import yt_dlp

    item = Item(group='YouTube', priority=0, title=title or url, url=url, digest=spec['name'])
    eid = digest._episode_id(spec['name'], item)
    existing = next((ep for ep in store.all_episodes(spec['name']) if ep.id == eid), None)
    if existing:
        return existing
    audio_dir = resolve(spec, spec['paths']['audio_dir'])
    audio_dir.mkdir(parents=True, exist_ok=True)
    options = {
        'format': 'bestaudio/best', 'noplaylist': True,
        'outtmpl': str(audio_dir / f'{eid}.%(ext)s'),
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '96'}],
        'retries': 3, 'socket_timeout': 30,
        'js_runtimes': {'node': {}},
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
    audio = audio_dir / f'{eid}.mp3'
    if not audio.exists() or audio.stat().st_size == 0:
        raise RuntimeError('YouTube download did not produce an audio file')
    ep = Episode(
        id=eid, digest=spec['name'], title=title or info['title'],
        description=f"Original audio from {info.get('channel') or info.get('uploader') or 'YouTube'}.\n\nSource: {url}",
        script='', audio_path=str(audio), kind='full', number=store.next_number(spec['name']),
        duration=tts.measure_duration(audio) or tts._hms(info.get('duration') or 0),
        annotations={'mode': 'original_audio', 'sources': [url]},
    )
    store.save_episode(ep)
    store.mark_seen(item.uid, spec['name'], item.group, ep.title, url)
    digest._write_feed(spec, store)
    return ep
