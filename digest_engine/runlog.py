"""Emit a machine- and human-readable record of every run.

This is what lets a domain repo keep a complete log of intermediate
artifacts and changes. Annotations are opaque to the engine: if a domain
layer puts evidence-graded claims there, we render them; otherwise we dump.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve


def new_run_id(digest: str) -> str:
    return f"{digest}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"


def _claims_table(claims: list[dict]) -> list[str]:
    lines = ["| claim | support | conf | sources |", "|---|---|---|---|"]
    for c in claims:
        srcs = ", ".join(c.get("sources", []) or []) or "—"
        lines.append(f"| {c.get('text','')} | `{c.get('support','')}` | {c.get('confidence','')} | {srcs} |")
    return lines


def write_record(spec: dict, record: dict) -> tuple[Path, Path]:
    runs = resolve(spec, spec["paths"].get("runs_dir", "output/runs"))
    runs.mkdir(parents=True, exist_ok=True)
    rid = record["run_id"]

    json_path = runs / f"{rid}.json"
    json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    lines: list[str] = [
        f"# Run {rid}",
        "",
        f"- digest: `{record.get('digest')}`",
        f"- kind: {record.get('kind')}",
        f"- started: {record.get('started')}",
        f"- finished: {record.get('finished')}",
        f"- collected: {len(record.get('collected', []))} item(s)",
        f"- scored: {len(record.get('scored', []))} item(s)",
        "",
    ]

    if record.get("scored"):
        lines += ["## Top scored items", ""]
        for s in record["scored"][:12]:
            lines.append(f"- `{s['score']:>5}` **{s['group']}** — {s['title']}  \n  _{s.get('reason','')}_")
        lines.append("")

    for ep in record.get("episodes", []):
        lines += [f"## {ep['title']}  `({ep['kind']}, {ep.get('duration','')})`", ""]
        ann = ep.get("annotations") or {}
        if ann.get("claims"):
            lines += ["### Claims", "", *_claims_table(ann["claims"]), ""]
        if ann.get("scrub"):
            sc = ann["scrub"]
            lines.append(f"- scrub: removed {len(sc.get('removed', []))}, "
                         f"replaced {len(sc.get('replaced', []))}, llm={sc.get('llm_pass')}")
            for r in sc.get("removed", [])[:10]:
                lines.append(f"  - removed: _{r}_")
            lines.append("")
        if ann.get("research"):
            lines += ["<details><summary>research context</summary>", "", "```",
                      str(ann["research"])[:4000], "```", "", "</details>", ""]
        elif ann and not ann.get("claims"):
            lines += ["<details><summary>annotations</summary>", "", "```json",
                      json.dumps(ann, indent=2, default=str)[:2000], "```", "", "</details>", ""]

    md_path = runs / f"{rid}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path