"""Per-episode feedback: capture it, then compile it into guidance.

The loop:
  1. Every episode gets a stable id and a feedback link.
  2. You (or a cofounder) capture feedback via the CLI, or by dropping a YAML
     file in feedback/inbox/, or by replying to the email digest.
  3. `compile_guidance` turns raw feedback into a short guidance file that the
     research/script prompts read on the next run.
  4. Corrections are also applied back into the knowledge base by the domain
     layer (see competitor-intel/scripts/apply_feedback.py).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

from .models import Feedback
from .store import Store


def capture(store: Store, feedback_dir: str | Path, episode_id: str, *, rating: int = 0,
            accuracy: str = "", tags: list[str] | None = None, note: str = "") -> Feedback:
    fb = Feedback(episode_id=episode_id, rating=rating, accuracy=accuracy,
                  tags=tags or [], note=note)
    store.save_feedback(fb)

    inbox = Path(feedback_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    out = inbox / f"{episode_id}-{fb.created.strftime('%Y%m%d%H%M%S')}.yaml"
    out.write_text(yaml.safe_dump({
        "episode_id": episode_id, "rating": rating, "accuracy": accuracy,
        "tags": fb.tags, "note": note, "created": fb.created.isoformat(),
    }, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return fb


def compile_guidance(store: Store, out_path: str | Path, seed_path: str | Path | None = None,
                     max_lines: int = 20) -> Path:
    """Summarise all feedback into a short, prompt-ready guidance file.

    A hand-written `seed_path` (if given) is preserved verbatim at the top so
    auto-compilation never destroys human guidance.
    """
    feedback = store.all_feedback()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seed = ""
    if seed_path and Path(seed_path).exists():
        seed = Path(seed_path).read_text(encoding="utf-8").rstrip() + "\n\n---\n\n"

    if not feedback:
        out_path.write_text(
            seed + "# Learned guidance\n\n_No feedback yet. Run `digest feedback` after an episode._\n",
            encoding="utf-8",
        )
        return out_path

    n = len(feedback)
    avg = sum(f.rating for f in feedback if f.rating) / max(1, sum(1 for f in feedback if f.rating))
    tags = Counter(t for f in feedback for t in f.tags)
    accuracy_issues = [f for f in feedback if f.accuracy == "issues"]
    notes = [f.note.strip() for f in feedback if f.note.strip()]

    lines = [
        "# Learned guidance (auto-compiled from listener feedback)",
        f"_{n} feedback item(s); average rating {avg:.1f}/5._",
        "",
        "## Recurring themes",
    ]
    for tag, count in tags.most_common(10):
        lines.append(f"- `{tag}` ({count}x)")
    if not tags:
        lines.append("- (none tagged yet)")

    if accuracy_issues:
        lines += ["", "## Accuracy problems flagged — be more careful here"]
        for f in accuracy_issues[:10]:
            lines.append(f"- [{f.episode_id}] {f.note or 'flagged as inaccurate'}")

    if notes:
        lines += ["", "## Recent notes to honour"]
        for note in notes[-max_lines:]:
            lines.append(f"- {note}")

    lines += [
        "",
        "## Standing instructions",
        "- Keep assuming the listener knows the field; lead with differences, not definitions.",
        "- Always state whether a claim is validated, third-party, or merely a vendor claim.",
    ]
    out_path.write_text(seed + "\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def read_guidance(guidance_path: str | Path) -> str:
    p = Path(guidance_path)
    return p.read_text(encoding="utf-8") if p.exists() else ""