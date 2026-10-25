"""Command line: `digest run|publish|sync|notify|feed|feedback|learn`."""
from __future__ import annotations

import argparse
import sys

from . import digest as digest_mod
from . import feedback as fb
from . import notify as notify_mod
from . import site as site_mod
from .config import base_url, load_dotenv, load_spec, resolve
from .models import Episode
from .store import Store


def _store_for(spec_path: str):
    spec = load_spec(spec_path)
    return spec, Store(resolve(spec, spec["paths"]["db_file"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digest", description="digest-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="narrate an article URL and publish it to the existing feed")
    p_add.add_argument("spec")
    p_add.add_argument("url")
    p_add.add_argument("--title", default="")
    p_add.add_argument("--no-push", action="store_true")

    p_inbox = sub.add_parser("inbox", help="process article URLs received through ntfy")
    p_inbox.add_argument("spec")

    p_run = sub.add_parser("run", help="collect + generate episodes")
    p_run.add_argument("spec")
    p_run.add_argument("--weekly", action="store_true")
    p_run.add_argument("--limit", type=int, default=0,
                       help="cap how many episodes this run makes (newest first)")
    p_run.add_argument("--catch-up", action="store_true",
                       help="mark everything currently in the sources as seen, without narrating")

    p_pub = sub.add_parser("publish", help="build the static site and push it to GitHub Pages")
    p_pub.add_argument("spec")
    p_pub.add_argument("--no-push", action="store_true", help="build the site but do not push")

    p_sync = sub.add_parser("sync", help="run, then publish, then notify")
    p_sync.add_argument("spec")
    p_sync.add_argument("--limit", type=int, default=0)
    p_sync.add_argument("--no-push", action="store_true")
    p_sync.add_argument("--no-notify", action="store_true")

    p_notify = sub.add_parser("notify", help="send a test push notification")
    p_notify.add_argument("spec")

    p_feed = sub.add_parser("feed", help="rebuild the podcast feed from the ledger")
    p_feed.add_argument("spec")

    p_fb = sub.add_parser("feedback", help="record feedback for an episode")
    p_fb.add_argument("spec")
    p_fb.add_argument("episode_id")
    p_fb.add_argument("--rating", type=int, default=0)
    p_fb.add_argument("--accuracy", default="", choices=["", "accurate", "issues"])
    p_fb.add_argument("--tag", action="append", default=[])
    p_fb.add_argument("--note", default="")

    p_learn = sub.add_parser("learn", help="compile feedback into guidance")
    p_learn.add_argument("spec")

    args = parser.parse_args(argv)
    load_dotenv()

    # The daily sync and URL submissions share a ledger and publish checkout.
    import fcntl
    spec = load_spec(args.spec)
    lock_path = resolve(spec, spec["paths"]["db_file"]).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _execute(args)


def _execute(args) -> int:
    if args.cmd == "inbox":
        from . import inbox
        return inbox.process(args.spec)
    elif args.cmd == "add":
        from . import resource
        try:
            ep = resource.add(args.spec, args.url, title=args.title)
            site_mod.publish(args.spec, push_site=not args.no_push)
            print(f"Ready: {ep.title}")
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"Could not add article: {exc}", file=sys.stderr)
            return 1
    elif args.cmd == "run":
        digest_mod.run(args.spec, weekly=args.weekly, limit=args.limit,
                       catch_up=args.catch_up)
    elif args.cmd == "publish":
        site_mod.publish(args.spec, push_site=not args.no_push)
    elif args.cmd == "sync":
        episodes = digest_mod.run(args.spec, weekly=True, limit=args.limit)
        site_mod.publish(args.spec, push_site=not args.no_push)
        # Only after a successful publish: the link has to work when it is tapped.
        if episodes and not args.no_notify:
            spec = load_spec(args.spec)
            notify_mod.announce(spec, episodes, base_url(spec))
    elif args.cmd == "notify":
        spec = load_spec(args.spec)
        probe = Episode(id="test", digest=spec["name"],
                        title=f"Test · {spec.get('title', 'Digest')}",
                        description="", script="", audio_path="test.mp3", duration="00:08:12")
        if not notify_mod.announce(spec, [probe], base_url(spec)):
            print("no notification sent; check `notify.ntfy.topic` in the spec")
            return 1
    elif args.cmd == "feed":
        digest_mod.rebuild_feed(args.spec)
    elif args.cmd == "feedback":
        spec, store = _store_for(args.spec)
        fb.capture(store, resolve(spec, "feedback"), args.episode_id,
                   rating=args.rating, accuracy=args.accuracy, tags=args.tag, note=args.note)
        fb.compile_guidance(store, resolve(spec, spec["feedback"]["guidance"]),
                            seed_path=resolve(spec, spec["feedback"].get("seed", "feedback/seed.md")))
        print(f"feedback recorded for {args.episode_id}; guidance updated")
    elif args.cmd == "learn":
        spec, store = _store_for(args.spec)
        out = fb.compile_guidance(store, resolve(spec, spec["feedback"]["guidance"]),
                                  seed_path=resolve(spec, spec["feedback"].get("seed", "feedback/seed.md")))
        print(f"guidance written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
