"""Scrub AI tells and filler out of a script before it is narrated.

Two passes:
  1. deterministic  - a curated list of clichés, filler sentences, lead-ins,
     and inflated word choices. Fast, offline, and it records exactly what it
     changed so the run log shows the diff.
  2. optional LLM   - a strict de-slop rewrite (`scrub.llm: true` in the spec),
     followed by another deterministic pass to catch anything it introduces.

The goal is narration that sounds like a person, not a language model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Whole sentences that are pure fluff — dropped outright.
BANNED_SENTENCES = [
    "and that is the whole story", "and thats the whole story",
    "and that is the whole point", "and thats the whole point",
    "and that is the key", "and that is the bottom line",
    "and that changes everything", "and that says it all",
    "the rest is history", "and the list goes on",
    "the possibilities are endless", "and that is that",
    "in today's fast-paced world", "in todays fast paced world",
    "let's dive in", "lets dive in", "let's dive right in", "buckle up",
    "here's the thing", "heres the thing", "make no mistake",
    "needless to say", "it goes without saying", "when all is said and done",
]

# Sentence-leading filler — stripped from the start of any sentence.
LEADING_FILLER = [
    r"it(?:'s| is)\s+(?:important|worth)\s+(?:to note|noting)(?: that)?\s*",
    r"the bottom line is(?: that)?\s*",
    r"needless to say,?\s*",
    r"at the end of the day,?\s*",
    r"when all is said and done,?\s*",
    r"here(?:'s| is) the thing[:,]?\s*",
    r"it should be noted that\s*",
    r"as a matter of fact,?\s*",
]

# Inline phrases — removed wherever they appear. Order matters.
BANNED_INLINE = [
    # trailing clauses, with any comma/space in front
    r"\s*[,—]?\s*and that(?:'s| is| are)\s+(?:the whole (?:story|point)|the bottom line|the key)\b",
    r"\s*[,—]?\s*and that changes everything\b",
    r"\s*[,—]?\s*and that says it all\b",
    # generic clichés
    r"\s*(?:that|this|it|which)?\s*is worth (?:naming|flagging|noting|calling out)(?: out loud)?\b",
    r"\bworth (?:naming|flagging|noting|calling out) out loud\b",
    r"\bin the realm of\b", r"\bin the world of\b",
    r"\bnavigate the (?:complexities|landscape) of\b",
    r"\bwe(?:'ve| have) got you covered\b",
    r"\bbe that as it may\b",
]

# Inflated word choices -> plain English.
# Inflated word choices -> plain English. Deliberately tense- and
# article-safe: every inflection maps to the matching inflection, and no
# adjective is swapped in a way that breaks "a/an" agreement. Anything that
# could reword an evidence hedge is excluded (review §2.1).
REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bdelves into\b", "examines"), (r"\bdelved into\b", "examined"),
    (r"\bdelving into\b", "examining"), (r"\bdelve into\b", "examine"),
    (r"\bdives into\b", "examines"), (r"\bdived into\b", "examined"),
    (r"\bdiving into\b", "examining"), (r"\bdive into\b", "examine"),
    (r"\butilizes\b", "uses"), (r"\butilized\b", "used"),
    (r"\butilizing\b", "using"), (r"\butilize\b", "use"),
    (r"\bleverages\b", "uses"), (r"\bleveraged\b", "used"),
    (r"\bleveraging\b", "using"), (r"\bleverage\b", "use"),
    (r"\bin order to\b", "to"),
    (r"\bprior to\b", "before"),
    (r"\ba myriad of\b", "many"), (r"\bmyriad of\b", "many"), (r"\bmyriad\b", "many"),
    (r"\bnumerous\b", "many"),
    (r"\bcommences\b", "starts"), (r"\bcommenced\b", "started"),
    (r"\bcommencing\b", "starting"), (r"\bcommence\b", "start"),
    (r"\bfacilitates\b", "helps"), (r"\bfacilitated\b", "helped"),
    (r"\bfacilitating\b", "helping"), (r"\bfacilitate\b", "help"),
    (r"\bsheds light on\b", "explains"), (r"\bshed light on\b", "explain"),
    (r"\bembarks on\b", "starts"), (r"\bembarked on\b", "started"),
    (r"\bembark on\b", "start"),
    (r"\bsuper important\b", "important"), (r"\bvery unique\b", "unique"),
    (r"\bin the not-too-distant future\b", "soon"),
]


@dataclass
class ScrubResult:
    text: str
    removed: list[str] = field(default_factory=list)
    replaced: list[tuple[str, str]] = field(default_factory=list)
    llm_pass: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.replaced or self.llm_pass)


def _normalize(sentence: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", "", sentence.lower()).strip()


def _capitalize_sentences(text: str) -> str:
    return re.sub(r"(^|[.!?]\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), text)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)      # space before punctuation
    text = re.sub(r",\s*([.!?])", r"\1", text)         # ",." -> "."
    text = re.sub(r",\s*,", ", ", text)
    text = re.sub(r"([,.!?;:])\1+", r"\1", text)       # doubled punctuation
    text = re.sub(r"^\s*[,;:]\s*", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _capitalize_sentences(text)


def _scrub_paragraph(para: str, removed: list[str]) -> str:
    # drop standalone fluff sentences
    sentences = re.split(r"(?<=[.!?])\s+", para)
    kept: list[str] = []
    for sent in sentences:
        if _normalize(sent) in BANNED_SENTENCES:
            removed.append(sent.strip())
            continue
        kept.append(sent)
    text = " ".join(kept)

    # strip sentence-leading filler
    def _strip_leading(sent: str) -> str:
        for pat in LEADING_FILLER:
            m = re.match(pat, sent, re.I)
            if m:
                removed.append(m.group(0).strip())
                sent = sent[m.end():]
        return sent

    kept2: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        s = _strip_leading(sent.strip())
        if s:
            kept2.append(s)
    text = " ".join(kept2)

    # strip inline phrases
    for pattern in BANNED_INLINE:
        for match in re.finditer(pattern, text, re.I | re.M):
            removed.append(match.group(0).strip())
        text = re.sub(pattern, "", text, flags=re.I | re.M)
    return text


def scrub(text: str, aggressive: bool = False) -> ScrubResult:
    removed: list[str] = []
    replaced: list[tuple[str, str]] = []

    paragraphs = re.split(r"\n{2,}", text)
    paragraphs = [_scrub_paragraph(p, removed) for p in paragraphs]
    text = "\n\n".join(paragraphs)

    for pattern, repl in REPLACEMENTS:
        new, n = re.subn(pattern, repl, text, flags=re.I)
        if n:
            replaced.append((pattern, repl))
            text = new

    # em-dash smoothing (a strong AI tell)
    text = re.sub(r"\s*—\s*", ", ", text)

    return ScrubResult(text=_tidy(text).strip(), removed=removed, replaced=replaced)


_DESLOP_SYSTEM = (
    "You are a ruthless copy editor removing AI writing tells from narration. "
    "Output only the rewritten text, nothing else."
)

_DESLOP_PROMPT = """Rewrite the narration as a DENSE, EASY-TO-FOLLOW summary of what
changed, in plain technical English, for a listener who already knows these
companies. The listener should finish and know exactly what changed. Nothing else.

Write for the ear:
- Short sentences. One idea each. Common words.
- Move between items with transition words: "First," "Second," "Also,"
  "Meanwhile," "Finally."
- Keep it compact. Cut anything that is not a change.

Delete:
- Negative parallelism in every form: "X, not Y", "not X but Y", "rather than",
  "it's not X, it's Y", "isn't just". State the fact plainly.
- ALL commentary about evidence or method: "corroborated, not company claims",
  "that split is our inference", "the caveat worth keeping", "not evidence of",
  "our claim is...". For a company claim, just write "says", "announced", or
  "according to".
- Any review or opinion of the company's marketing.
- Signposting: "Start with", "So here's", "worth noting", "the interesting part".
- Second person, em-dashes, filler adverbs, inflated words
  (utilize/leverage -> use; delve into -> examine).

Keep EXACTLY:
- Numbers, dates and identifiers, in the exact form given.
- Names and product names.

Output only the rewritten narration.

Narration:
---
{text}
---
"""


def deslop(text: str, model: str) -> str:
    """Optional LLM de-slop pass. Returns the original text on any failure."""
    try:
        from .llm import complete

        out = complete(model, _DESLOP_SYSTEM, _DESLOP_PROMPT.format(text=text), temperature=0.3)
        return out.strip() or text
    except Exception as exc:
        print(f"  ! deslop pass skipped: {exc}")
        return text


def scrub_script(text: str, cfg: dict | None = None) -> ScrubResult:
    """Full scrub pipeline driven by a spec's `scrub` block."""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return ScrubResult(text=text)

    result = scrub(text, aggressive=cfg.get("aggressive", False))
    if cfg.get("llm"):
        model = cfg.get("model") or cfg.get("_model") or "openrouter/deepseek/deepseek-v4.1-flash"
        result.text = scrub(deslop(result.text, model)).text
        result.llm_pass = True
    return result