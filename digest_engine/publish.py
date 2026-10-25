"""Output sinks: podcast RSS, email, markdown."""
from __future__ import annotations

import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import feedback_url
from .models import Episode

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST = "https://podcastindex.org/namespace/1.0"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("podcast", PODCAST)
ET.register_namespace("atom", ATOM)


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = text
    return el


def aware(dt: datetime) -> datetime:
    """Feeds mix item pubDates and run timestamps; normalise before sorting."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rfc822(dt: datetime) -> str:
    return format_datetime(aware(dt))


def _source_link(ep: Episode) -> str:
    srcs = (ep.annotations or {}).get("sources") or []
    return srcs[0] if srcs else ""


def _page(ep: Episode) -> str:
    """The episode's own web page (transcript, sources, discussion), if a domain
    layer published one and recorded it as `annotations["page"]`."""
    page = (ep.annotations or {}).get("page") or ""
    return page if page.startswith(("https://", "http://")) else ""


def podcast_feed(episodes: list[Episode], spec: dict, base: str, feed_path: Path,
                 max_items: int = 0) -> Path:
    """Write the RSS feed. Episodes go out newest-first, capped at `max_items`."""
    episodes = sorted(episodes, key=lambda e: (aware(e.published), e.number), reverse=True)
    if max_items:
        episodes = episodes[:max_items]
    rss = ET.Element("rss", {"version": "2.0"})
    ch = _sub(rss, "channel")
    _sub(ch, "title", spec.get("title", "Digest"))
    _sub(ch, "link", base)
    _sub(ch, "description", spec.get("description", ""))
    _sub(ch, "language", spec.get("language", "en-us"))
    last_change = max((aware(e.published) for e in episodes), default=datetime.now(timezone.utc))
    _sub(ch, "lastBuildDate", _rfc822(last_change))
    _sub(ch, "generator", "digest-engine")
    ET.SubElement(ch, f"{{{ATOM}}}link", {"href": f"{base}/feed.xml", "rel": "self", "type": "application/rss+xml"})
    _sub(ch, f"{{{ITUNES}}}author", spec.get("author", ""))
    _sub(ch, f"{{{ITUNES}}}explicit", "false")
    cover = spec.get("cover_image")
    if cover:
        ET.SubElement(ch, f"{{{ITUNES}}}image", {"href": cover})
        img = _sub(ch, "image")
        _sub(img, "url", cover)
        _sub(img, "title", spec.get("title", "Digest"))
        _sub(img, "link", base)

    for ep in episodes:
        item = _sub(ch, "item")
        _sub(item, "title", ep.title)
        desc = ep.description
        page = _page(ep)
        furl = ep.feedback_url or feedback_url(spec, ep.id)
        if page:
            desc = f"{desc}\n\nRead, discuss and give feedback: {page}"
        elif furl:
            desc = f"{desc}\n\nSend feedback: {furl}"
        _sub(item, "description", desc)
        _sub(item, f"{{{ITUNES}}}summary", ep.description)
        _sub(item, "pubDate", _rfc822(ep.published))
        _sub(item, "guid", ep.id, isPermaLink="false")
        audio = Path(ep.audio_path)
        url = f"{base}/audio/{audio.name}"
        try:
            length = audio.stat().st_size
        except OSError:
            length = 0
        ET.SubElement(item, "enclosure", {"url": url, "length": str(length), "type": "audio/mpeg"})
        _sub(item, f"{{{ITUNES}}}duration", ep.duration)
        _sub(item, f"{{{ITUNES}}}episode", str(ep.number))
        _sub(item, f"{{{ITUNES}}}explicit", "false")
        _sub(item, f"{{{PODCAST}}}episodeType", "bonus" if ep.kind == "urgent" else "full")
        link = page or _source_link(ep) or ep.feedback_url
        if link:
            _sub(item, "link", link)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(feed_path, encoding="utf-8", xml_declaration=True)
    return feed_path


def markdown(text: str, out_path: Path, title: str = "") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = text if not title else f"# {title}\n\n{text}\n"
    out_path.write_text(body, encoding="utf-8")
    return out_path


def email(text: str, subject: str, to: str | None = None, html: str | None = None) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "") or os.environ.get("SMTP_PASS", "")
    to = to or os.environ.get("EMAIL_TO", "")
    recipients = [a.strip() for a in re.split(r"[,;\s]+", to) if a.strip()]
    if not (user and password and recipients):
        print("  ! email sink skipped: SMTP_USER/SMTP_PASSWORD/EMAIL_TO not set")
        return
    if html:
        msg: MIMEText | MIMEMultipart = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, recipients, msg.as_string())
    print(f"  ✉ email sent to {', '.join(recipients)}")


def ntfy(title: str, message: str, topic: str, click: str = "", tags: str = "loudspeaker") -> None:
    """Push a notification to ntfy.sh (the topic acts as the secret)."""
    if not topic:
        print("  ! ntfy sink skipped: no topic (set NTFY_TOPIC or spec.ntfy.topic)")
        return
    import urllib.request

    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": str(title).encode("ascii", "ignore").decode(),
                 "Tags": tags, "Priority": "default"},
        method="POST",
    )
    if click:
        req.add_header("Click", click)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    print(f"  \U0001f514 ntfy -> {topic}")


SINKS = {"podcast_rss", "email", "markdown", "ntfy"}