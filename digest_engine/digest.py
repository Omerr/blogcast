"""Orchestrator: a Digest run.

    spec = load_spec("digest.yaml")
    run(spec, weekly=True)

Topic-neutral: collect -> extract -> (score) -> compose -> scrub -> narrate ->
publish. A domain layer can override the compose step via `hooks.compose`.

Two shapes of run:

  * **digest** (default) — the selected items are scored and folded into one
    `weekly` (and optionally one `urgent`) episode.
  * **per-item** (`format.per_item: true`) — every new item becomes its own
    episode, in publication order. This is the "read my blogs aloud" shape:
    no scoring, no LLM, one post = one episode.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import compose as compose_mod
from . import feedback as fb
from . import llm, publish, runlog, scrub, sources as src_mod, tts
from .config import base_url, feedback_url, load_dotenv, load_spec, resolve
from .enrich import dedupe, extract
from .models import Episode, utcnow
from .store import Store

DEFAULT_MAX_AGE_DAYS = 30


def _max_age(spec: dict) -> timedelta:
    return timedelta(days=int(spec["curation"].get("max_age_days", DEFAULT_MAX_AGE_DAYS)))


def _recent(item, max_age: timedelta) -> bool:
    return item.published is None or (datetime.now(timezone.utc) - item.published) <= max_age


def collect_new(spec: dict, store: Store) -> list:
    max_age = _max_age(spec)
    items = [i for i in src_mod.collect(spec["sources"], spec["name"])
             if i.title and i.url and not store.is_seen(i.uid) and _recent(i, max_age)]
    return dedupe(items)


def _episode_id(digest_name: str, item) -> str:
    """Stable per-item id, so a re-run never produces a duplicate episode."""
    slug = re.sub(r"[^a-z0-9]+", "-", item.url.rstrip("/").rsplit("/", 1)[-1].lower()).strip("-")[:60]
    fingerprint = hashlib.sha1(item.url.encode("utf-8")).hexdigest()[:6]
    return f"{digest_name}-{slug or 'item'}-{fingerprint}"


def _persist_source_health(spec: dict) -> None:
    """Persist per-source counts; shout when a source goes silent twice running.

    A priority-0 competitor's feed returning 403 for weeks must not present as
    a calm, empty digest (review §2.10).
    """
    health = src_mod.health_snapshot()
    if not health:
        return
    path = resolve(spec, spec["paths"].get("health_file", "output/source_health.json"))
    prior = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    for key, rec in health.items():
        if rec.get("last_count", 0) == 0:
            rec["runs_zero"] = int(prior.get(key, {}).get("runs_zero", 0)) + 1
        else:
            rec["runs_zero"] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(health, indent=2), encoding="utf-8")
    for key, rec in health.items():
        if rec.get("runs_zero", 0) >= 2:
            print(f"  ! source silent for {rec['runs_zero']} runs: {key}")


def run(spec_path: str, *, weekly: bool = False, limit: int = 0,
        catch_up: bool = False, max_enrich: int = 20) -> list[Episode]:
    load_dotenv()
    spec = load_spec(spec_path)
    store = Store(resolve(spec, spec["paths"]["db_file"]))
    curation = spec["curation"]
    fmt = spec.get("format", {}) or {}
    mode = fmt.get("mode", "summary")
    per_item = bool(fmt.get("per_item", False))

    items = collect_new(spec, store)
    _persist_source_health(spec)
    shape = "per-item" if per_item else "digest"
    print(f"[{spec['name']}] {len(items)} new item(s)  (mode: {mode}, {shape})")
    if not items:
        return []

    if catch_up:
        for it in items:
            store.mark_seen(it.uid, spec["name"], it.group, it.title, it.url)
        print(f"  marked {len(items)} item(s) as seen; no episodes produced")
        return []

    if per_item:
        episodes, scored = _run_per_item(items, spec, store, limit)
    else:
        episodes, scored = _run_digest(items, spec, store, weekly, mode, curation, max_enrich)

    _write_feed(spec, store)
    sink_results = _dispatch_sinks(spec, episodes)

    record = {
        "run_id": runlog.new_run_id(spec["name"]),
        "digest": spec["name"],
        "started": datetime.now(timezone.utc).isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
        "kind": f"{mode}/{shape}",
        "sinks": sink_results,
        "collected": [{"group": i.group, "title": i.title, "url": i.url,
                       "source_type": i.source_type, "published": i.published} for i in items],
        "scored": [{"group": it.group, "title": it.title, "url": it.url,
                    "score": s, "reason": r} for it, s, r in scored],
        "episodes": [{
            "id": ep.id, "title": ep.title, "kind": ep.kind, "duration": ep.duration,
            "audio": ep.audio_path, "script": ep.script, "annotations": ep.annotations,
        } for ep in episodes],
    }
    jpath, mpath = runlog.write_record(spec, record)
    print(f"  run log: {jpath.name} / {mpath.name}")
    return episodes


def _run_per_item(items, spec, store, limit):
    """One episode per item, oldest first, so episode numbers follow time."""
    guidance = fb.read_guidance(resolve(spec, spec["feedback"]["guidance"])) if spec["feedback"]["enabled"] else ""
    persona = spec.get("persona", "")
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    items = sorted(items, key=lambda i: (i.published or far_past))
    if limit > 0:
        items = items[-limit:]

    episodes: list[Episode] = []
    for it in items:
        eid = _episode_id(spec["name"], it)
        if store.has_episode(eid):
            store.mark_seen(it.uid, spec["name"], it.group, it.title, it.url)
            continue
        if not it.content:
            it.content = extract(it.url)
        fetch_failed = not it.content
        try:
            ep = _make_episode([it], spec, persona, guidance, "full", store,
                               episode_id=eid, published=it.published)
        except Exception as exc:  # one dud article shouldn't kill the run
            print(f"  ! skipped [{it.group}] {it.title[:60]}: {exc}")
            # A post that is genuinely too short will never grow; stop retrying it.
            # A post we could not fetch might come back, so leave it unseen.
            if not fetch_failed:
                store.mark_seen(it.uid, spec["name"], it.group, it.title, it.url)
            continue
        episodes.append(ep)
        # Only mark seen once the episode exists, so a crash mid-run retries.
        store.mark_seen(it.uid, spec["name"], it.group, it.title, it.url)

    scored = [(it, 0.0, "per-item") for it in items]
    return episodes, scored


def _run_digest(items, spec, store, weekly, mode, curation, max_enrich):
    items.sort(key=lambda i: (i.priority, -(i.published.timestamp() if i.published else 0)))
    for it in items[:max_enrich]:
        if not it.content:
            it.content = extract(it.url)

    guidance = fb.read_guidance(resolve(spec, spec["feedback"]["guidance"])) if spec["feedback"]["enabled"] else ""
    persona = spec.get("persona", "")

    try:
        scored = llm.score_items(items, persona, curation["model"], guidance)
        for it, score, reason in scored[:12]:
            print(f"  {score:>5.1f}  [{it.group}] {it.title[:65]}  ({reason})")
    except Exception as exc:
        # Never hardcode which providers exist (review §2.7): attempt the call
        # and fall back only for genuine failures.
        if mode == "full":
            print(f"  (scoring unavailable: {exc}); full-read mode continues")
            scored = [(it, 0.0, "unscored") for it in items]
        else:
            print(f"  ! LLM scoring failed: {exc}")
            return [], []

    urgent = [(it, s) for it, s, _ in scored if it.priority <= 1 and s >= curation["urgent_threshold"]]
    weekly_items = [(it, s) for it, s, _ in scored if s >= curation["weekly_threshold"]][:curation["max_items_per_weekly"]]
    if mode == "full" and not weekly_items:
        weekly_items = [(it, s) for it, s, _ in scored][:curation["max_items_per_weekly"]]

    selected = []
    episodes: list[Episode] = []
    if urgent:
        selected.append(urgent[0][0])
        episodes.append(_make_episode([urgent[0][0]], spec, persona, guidance, "urgent", store))
    if weekly and weekly_items:
        selected.extend(it for it, _ in weekly_items)
        episodes.append(_make_episode([it for it, _ in weekly_items], spec, persona, guidance, "weekly", store))
    # Mark seen only what actually became an episode (review §2.6). Unselected
    # items stay unseen, so a later, better-tuned run can still catch them.
    for it in selected:
        store.mark_seen(it.uid, spec["name"], it.group, it.title, it.url)
    return episodes, scored


def _make_episode(selected, spec, persona, guidance, kind, store, *,
                  episode_id: str | None = None, published=None) -> Episode:
    title, description, script, annotations = compose_mod.compose(
        selected, spec, persona, guidance, kind
    )

    scrub_cfg = dict(spec.get("scrub", {}) or {})
    scrub_cfg.setdefault("_model", spec["curation"]["model"])
    result = scrub.scrub_script(script, scrub_cfg)
    if result.changed:
        print(f"  ✂ scrub: removed {len(result.removed)}, replaced {len(result.replaced)}"
              + (", +LLM de-slop" if result.llm_pass else ""))
    script = result.text
    annotations = {**annotations, "scrub": {
        "removed": result.removed, "replaced": [list(r) for r in result.replaced],
        "llm_pass": result.llm_pass,
    }}
    # Which guidance the episode ran under, so feedback can be attributed to the
    # guidance that produced it.
    annotations["guidance_sha"] = hashlib.sha256((guidance or "").encode("utf-8")).hexdigest()[:12]
    # A domain can re-derive claims from the FINAL script (the grader ran before
    # the de-slop rewrite), so the written receipts match the audio.
    regrade_hook = (spec.get("hooks") or {}).get("regrade")
    if regrade_hook:
        try:
            from .compose import load_hook
            new_claims = load_hook(regrade_hook)(script, selected, spec, annotations)
            if new_claims is not None:
                annotations["claims"] = new_claims
                print(f"  ✎ re-graded {len(new_claims)} claim(s) from the final script")
        except Exception as exc:
            print(f"  ! regrade hook failed: {exc}")

    eid = episode_id or f"{spec['name']}-{kind}-{utcnow():%Y%m%d-%H%M%S}"
    mp3 = resolve(spec, spec["paths"]["audio_dir"]) / f"{eid}.mp3"
    print(f"  ♪ synthesizing: {title}")
    tts.synthesize(script, mp3, spec["tts"])

    email_to = os.environ.get("EMAIL_TO", "")
    fb_url = feedback_url(spec, eid)

    duration = tts.measure_duration(mp3) or tts.estimate_duration(script, spec["tts"]["words_per_minute"])
    ep = Episode(
        id=eid, digest=spec["name"], title=title, description=description, script=script,
        audio_path=str(mp3), duration=duration,
        kind=kind, number=store.next_number(spec["name"]), annotations=annotations, feedback_url=fb_url,
    )
    if published is not None:
        ep.published = published
    store.save_episode(ep)
    return ep


def _digest_text(episodes: list[Episode]) -> str:
    lines = []
    for ep in episodes:
        lines.append(f"# {ep.title}\n")
        if ep.description:
            lines.append(ep.description + "\n")
        for c in (ep.annotations or {}).get("claims", []) or []:
            lines.append(f"- ({c.get('support', '?')}) {c.get('text', '')}")
        lines.append("")
    return "\n".join(lines)


def _dispatch_sinks(spec: dict, episodes: list[Episode]) -> list[dict]:
    """Run every declared sink, each guarded, and report outcomes.

    `podcast_rss` is already written by `_write_feed`; `email` and `markdown`
    were declared in specs and READMEs but never dispatched (review §2.6 / Q9).
    """
    sinks = spec.get("sinks", []) or []
    out_dir = resolve(spec, spec["paths"]["output_dir"])
    results: list[dict] = []
    for sink in sinks:
        try:
            if sink == "podcast_rss":
                results.append({"sink": sink, "ok": True})
            elif sink == "markdown":
                path = publish.markdown(_digest_text(episodes), out_dir / "digest.md", spec.get("title", "Digest"))
                results.append({"sink": sink, "ok": True, "path": str(path)})
            elif sink == "email":
                subject = f"{spec.get('title', 'Digest')} — {len(episodes)} new"
                text, html_text = _digest_text(episodes), None
                hook = (spec.get("hooks") or {}).get("render_email")
                if hook:
                    try:
                        from .compose import load_hook
                        subject, text, html_text = load_hook(hook)(episodes, spec)
                    except Exception as exc:
                        print(f"  ! render_email hook failed: {exc}")
                publish.email(text, subject, html=html_text)
                results.append({"sink": sink, "ok": True})
            elif sink == "ntfy":
                if episodes:
                    topic = (spec.get("ntfy", {}) or {}).get("topic") or os.environ.get("NTFY_TOPIC", "")
                    link = f"{base_url(spec)}/feed.xml"
                    titles = " \u00b7 ".join(ep.title for ep in episodes)
                    publish.ntfy(
                        f"{len(episodes)} new competitor episode{'s' if len(episodes) > 1 else ''}",
                        f"{titles}\n\nListen: {link}", topic, click=link)
                results.append({"sink": sink, "ok": True})
            else:
                results.append({"sink": sink, "ok": False, "error": "unknown sink"})
        except Exception as exc:
            print(f"  ! sink {sink} failed: {exc}")
            results.append({"sink": sink, "ok": False, "error": str(exc)})
    return results


def _write_feed(spec: dict, store: Store) -> None:
    episodes = store.all_episodes(spec["name"])
    publish.podcast_feed(episodes, spec, base_url(spec), resolve(spec, spec["paths"]["feed_file"]))


def rebuild_feed(spec_path: str) -> None:
    load_dotenv()
    spec = load_spec(spec_path)
    store = Store(resolve(spec, spec["paths"]["db_file"]))
    _write_feed(spec, store)
    print(f"feed rebuilt: {resolve(spec, spec['paths']['feed_file'])}")
