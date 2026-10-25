"""Offline tests — no network, no LLM, no API keys. CI-safe."""
from __future__ import annotations

from digest_engine import enrich, feedback, scrub, sources
from digest_engine.models import Feedback
from digest_engine.store import Store


# ---- scrub (review §2.1) --------------------------------------------------
def test_scrub_does_not_corrupt_tense_or_articles():
    out = scrub.scrub("X leverages agents and utilizes models.").text
    assert "uses agents" in out
    assert "leverages" not in out and "utilizes" not in out
    # the old bug turned "a cutting-edge" into "a advanced"
    assert "a advanced" not in scrub.scrub("a cutting-edge platform").text


def test_scrub_removes_cliche_and_filler():
    r = scrub.scrub("It is worth noting that this works. And that is the whole story.")
    assert "worth noting" not in r.text
    assert "whole story" not in r.text
    assert r.removed


# ---- collectors (review §2.10) -------------------------------------------
def test_sitemap_index_is_followed(monkeypatch):
    index = (b'<?xml version="1.0"?><sitemapindex '
             b'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             b'<sitemap><loc>https://e.com/s1.xml</loc></sitemap></sitemapindex>')
    child = (b'<?xml version="1.0"?><urlset '
             b'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             b'<url><loc>https://e.com/blog/post</loc><lastmod>2026-01-02</lastmod></url>'
             b'</urlset>')

    def fake_fetch(url, timeout=20):
        return index if url.endswith("sitemap.xml") else child

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    items = sources.collect_sitemap(
        {"url": "https://e.com/sitemap.xml", "group": "E", "priority": 0, "include": "/blog/"}, "t"
    )
    assert len(items) == 1
    assert items[0].url.endswith("/blog/post")


def test_fetch_failure_never_becomes_content(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("403 Forbidden")

    monkeypatch.setattr(enrich, "fetch", boom)
    assert enrich.extract("https://x.example") == ""


# ---- arak docsite collector (Stage C) ------------------------------------
def _arak_html(payload: dict) -> str:
    import base64 as b64
    import gzip
    import json as _json
    raw = b64.b64encode(gzip.compress(_json.dumps(payload).encode())).decode()
    return f'<script id="docs-payload" type="application/octet-stream">{raw}</script>'


def test_arak_decode_snapshot_and_diff():
    from digest_engine import arak

    base = {
        "shell": "x",
        "pages": {"index": "<h1>Hi</h1>",
                  "whats-new": "<h2>September 2026</h2><h3>Web app</h3>"
                               "<li>Adaptive scanning thing</li>"},
        "titles": {"index": "Home", "whats-new": "What's New"},
        "order": ["index", "whats-new"],
    }
    snap = arak.snapshot_of(arak.decode_payload(_arak_html(base)))
    assert "whats-new" in snap["nav"]
    assert len(snap["whats_new"]) == 1

    nxt = {
        "shell": "x",
        "pages": {**base["pages"],
                  "whats-new": "<h2>September 2026</h2><h3>Web app</h3>"
                               "<li>Adaptive scanning thing</li><li>Second new thing</li>",
                  "newpage": "<p>New capability</p>"},
        "titles": {**base["titles"], "newpage": "New Page"},
        "order": ["index", "whats-new", "newpage"],
    }
    snap2 = arak.snapshot_of(arak.decode_payload(_arak_html(nxt)))
    items = arak.diff(snap, snap2, "demos/x", "Terra", 0, "t",
                      pages=nxt["pages"], titles=nxt["titles"])
    titles = [i.title for i in items]
    assert any("added" in t and "New Page" in t for t in titles)
    assert any("Second new thing" in t for t in titles)
    # a baseline run emits nothing on its own
    assert arak.snapshot_of(arak.decode_payload(_arak_html(base)))["pages"]


def test_arak_page_collector(monkeypatch):
    from digest_engine import arak

    monkeypatch.setattr(arak, "_gh_read", lambda path: "<h1>Expert call</h1><p>They ship X. Also Y.</p>")
    items = arak.collect_arak_page({"path": "expert-calls/x", "group": "X", "priority": 0,
                                    "title": "X expert call", "provenance": "expert_call"}, "t")
    assert len(items) == 1
    it = items[0]
    assert it.source_type == "arak_page"
    assert it.meta["provenance"] == "expert_call"
    assert it.url.endswith("/expert-calls/x")
    assert "Expert call" in it.content


# ---- feedback seed preservation (review §2.2) ----------------------------
def test_guidance_preserves_seed(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save_feedback(Feedback(episode_id="e1", rating=4, note="be sharper"))
    seed = tmp_path / "seed.md"
    seed.write_text("# Standing guidance\n- never overwrite me\n")
    out = feedback.compile_guidance(store, tmp_path / "g.md", seed_path=seed)
    text = out.read_text()
    assert "never overwrite me" in text      # seed survived
    assert "Learned guidance" in text        # auto block appended


# ---- pronunciation map (tts) ---------------------------------------------
def test_apply_pronunciations():
    from digest_engine.tts import apply_pronunciations as a
    rules = {"SIEM": "sim", "SOC": "sock", "IT": "I T", "TIP": "T I P", "CVE": "C V E"}
    out = a("The SIEM and two SIEMs feed the SOC. CVE-2026-1234. IT teams; it works. A tip.", rules)
    assert "sim and two sims" in out
    assert "sock" in out
    assert "C V E-2026-1234" in out
    assert "I T teams; it works." in out          # lowercase 'it' untouched
    assert "A tip." in out                          # lowercase 'tip' untouched
    assert a("nothing to change here", rules) == "nothing to change here"


def test_feed_prefers_the_episode_page(tmp_path):
    """annotations['page'] becomes the item <link> and the feedback line."""
    from datetime import datetime, timezone
    from xml.etree import ElementTree as ET

    from digest_engine.models import Episode
    from digest_engine.publish import podcast_feed

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    common = dict(digest="d", description="Desc", script="s", audio_path=str(audio),
                  published=datetime(2026, 9, 24, tzinfo=timezone.utc), duration="00:01:00")
    with_page = Episode(id="e1", title="With page",
                        annotations={"page": "https://pages.example/brief-1"},
                        feedback_url="https://github.com/o/r/issues/new", **common)
    without = Episode(id="e2", title="Without page", feedback_url="https://github.com/o/r/issues/new",
                      annotations={"page": "javascript:alert(1)"}, **common)
    out = podcast_feed([with_page, without], {"title": "T"}, "https://feed.example", tmp_path / "feed.xml")
    items = {i.findtext("title"): i for i in ET.parse(out).getroot().iter("item")}
    assert items["With page"].findtext("link") == "https://pages.example/brief-1"
    assert "Read, discuss and give feedback: https://pages.example/brief-1" in items["With page"].findtext("description")
    # a non-http page is ignored; the old feedback link stays
    assert items["Without page"].findtext("link") == "https://github.com/o/r/issues/new"
    assert "Send feedback: https://github.com/o/r/issues/new" in items["Without page"].findtext("description")


def test_social_keeps_recent_own_posts_dated_from_the_id():
    from datetime import datetime, timezone
    from digest_engine import social
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    # X: 2102108130515521546 -> 2026-09-21; 2032094542174789832 -> 2026-03-12 (too old)
    x = [{"href": "https://x.com/Horizon3ai/status/2102108130515521546?s=20",
          "title": 'Horizon3.ai on X: "Cyberspace has become a machine-speed battlefield" / X', "body": "b"},
         {"href": "https://twitter.com/Horizon3ai/status/2102108130515521546", "title": "dup", "body": ""},
         {"href": "https://x.com/someoneelse/status/2102108130515521546", "title": "reply", "body": ""},
         {"href": "https://x.com/Horizon3ai/status/2032094542174789832", "title": "old", "body": ""}]
    items = social.collect_social({"platform": "x", "handle": "Horizon3ai", "group": "Horizon3.ai", "priority": 0},
                                  "d", search=lambda q, n: x, now=now, pause=0)
    assert [i.url for i in items] == ["https://x.com/Horizon3ai/status/2102108130515521546"]
    assert items[0].title == "Cyberspace has become a machine-speed battlefield"
    assert items[0].published.date().isoformat() == "2026-09-21" and items[0].source_type == "social"
    # LinkedIn: the activity id is ms since 1970 in the top bits. Keep the full URL (the suffix is needed).
    li = [{"href": "https://il.linkedin.com/posts/cormaai_announcing-corma-activity-7492612212130410496-bhme",
           "title": "Announcing Corma and our $60M seed | Corma", "body": "We raised"}]
    items = social.collect_social({"platform": "linkedin", "handle": "cormaai", "group": "Corma", "max_age_days": 400},
                                  "d", search=lambda q, n: li, now=now, pause=0)
    assert items[0].url == "https://www.linkedin.com/posts/cormaai_announcing-corma-activity-7492612212130410496-bhme"
    assert items[0].title == "Announcing Corma and our $60M seed"
    assert items[0].published.date().isoformat() == "2026-08-10"   # the $60M seed announcement
    # a snowflake decoding to the future is not a post
    assert social.post_time("x", str((10**14) << 22)) is None


def test_social_generic_titles_use_the_post_text():
    from digest_engine import social
    assert social._title("linkedin", "Mate's Post", "mate-security",
                         "Mate joins the AWS Marketplace. More soon.") == "Mate joins the AWS Marketplace."
    assert social._title("linkedin", "Real headline | Mate", "mate-security", "body") == "Real headline"
    assert social._title("x", 'Horizon3.ai on X: "" / X 23 Sep 2026 14:58:07', "Horizon3ai",
                         "NodeZero now tests Entra ID.") == "NodeZero now tests Entra ID."
    assert social._title("linkedin", "#aisoc #cybersecurity", "seven-ai", "7AI hires a CRO. Details.") == "7AI hires a CRO."


def test_social_require_keeps_only_posts_about_the_company():
    from datetime import datetime, timezone
    from digest_engine import social
    r = [{"href": "https://www.linkedin.com/posts/alon-pluda-48361b264_a-activity-7492612212130410496-aa",
          "title": "Alon Pluda's Post", "body": "Today we are announcing Corma."},
         {"href": "https://www.linkedin.com/posts/alon-pluda-48361b264_b-activity-7492612212130410497-bb",
          "title": "Alon Pluda's Post", "body": "Great hike this weekend."}]
    items = social.collect_social({"platform": "linkedin", "handle": "alon-pluda-48361b264", "group": "Corma",
                                   "require": r"\bCorma\b", "max_age_days": 400}, "d",
                                  search=lambda q, n: r, now=datetime(2026, 9, 25, tzinfo=timezone.utc), pause=0)
    assert [i.title for i in items] == ["Today we are announcing Corma."]
