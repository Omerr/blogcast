"""TTS providers. Cloud voices via LiteLLM; local via edge-tts.

Pronunciations
--------------
edge-tts XML-escapes its input and builds the SSML itself, so you cannot inject
<phoneme>/<say-as> tags. The portable way to fix a mispronounced term is a
**pronunciation map applied to the text just before synthesis**: the script keeps
"SIEM" (correct spelling, correct transcript) and the voice says "sim". This
works for every provider, local or cloud.

    tts:
      pronunciations:
        SIEM: sim
        CVE: "C V E"


Spec:
    tts: {provider: edge, voice: en-US-AndrewMultilingualNeural, rate: "+0%"}
    tts: {provider: litellm, model: openai/tts-1, voice: alloy}
    tts: {provider: litellm, model: gemini/gemini-2.5-flash-preview-tts, voice: Kore}
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path


def apply_pronunciations(text: str, rules: dict | None) -> str:
    """Replace terms with their spoken form, whole-word, **case-sensitively**,
    preserving a trailing plural "s"/"S" (SIEMs -> sims). Longest terms first so
    "CVE" wins over "CV".

    Case-sensitive on purpose: acronym keys collide with everyday words if
    ignored ("IT" -> "I T" would rewrite "it", "TIP" would rewrite "tip"). Keys
    are matched exactly as written, so write them in the case the text uses.
    """
    if not rules:
        return text
    for term in sorted(rules, key=len, reverse=True):
        repl = str(rules[term])
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}([sS]?)(?![A-Za-z0-9])")
        text = pat.sub(lambda m: repl + m.group(1), text)
    return text


def synthesize(text: str, out_path: str | Path, tts_cfg: dict) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = apply_pronunciations(text, tts_cfg.get("pronunciations") or {})
    provider = (tts_cfg.get("provider") or "edge").lower()

    if provider == "edge":
        import edge_tts

        async def _run() -> None:
            await edge_tts.Communicate(
                text, tts_cfg.get("voice", "en-US-AndrewMultilingualNeural"),
                rate=tts_cfg.get("rate", "+0%"),
            ).save(str(out_path))

        asyncio.run(_run())
    elif provider == "litellm":
        import litellm

        resp = litellm.speech(
            model=tts_cfg["model"], voice=tts_cfg.get("voice", "alloy"), input=text
        )
        resp.stream_to_file(str(out_path))
    else:
        raise ValueError(f"Unknown TTS provider: {provider!r}")

    return out_path


def _hms(seconds: int) -> str:
    h, rem = divmod(max(int(seconds), 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def estimate_duration(text: str, words_per_minute: int = 155) -> str:
    return _hms(max(len(text.split()), 1) / max(words_per_minute, 1) * 60)


# MPEG-1/2/2.5 Layer III header tables, indexed by the header's bitrate and
# sample-rate fields. Enough to read a CBR mp3's real length without ffmpeg.
_BITRATES_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_BITRATES_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def measure_duration(path: str | Path) -> str:
    """Real duration of a CBR mp3, or "" if the file cannot be read as one."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return ""

    offset = 0
    if data[:3] == b"ID3":                       # skip the ID3v2 tag
        size = data[6:10]
        if len(size) == 4:
            offset = 10 + ((size[0] << 21) | (size[1] << 14) | (size[2] << 7) | size[3])

    for i in range(offset, min(len(data) - 4, offset + 200_000)):
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            continue
        version = (data[i + 1] >> 3) & 0x03      # 3 = MPEG1, 2 = MPEG2, 0 = MPEG2.5
        layer = (data[i + 1] >> 1) & 0x03        # 1 = Layer III
        bitrate_idx = (data[i + 2] >> 4) & 0x0F
        rate_idx = (data[i + 2] >> 2) & 0x03
        if layer != 1 or version == 1 or rate_idx == 3 or bitrate_idx in (0, 15):
            continue
        kbps = (_BITRATES_V1 if version == 3 else _BITRATES_V2)[bitrate_idx]
        if not kbps:
            continue
        return _hms((len(data) - i) * 8 / (kbps * 1000))
    return ""