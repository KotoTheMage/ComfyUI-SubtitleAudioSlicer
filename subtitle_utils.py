"""Subtitle parsing + timestamp naming for SubtitleAudioSlicer.

A single lenient parser handles both SRT and WebVTT (and the SRT that ffmpeg
produces when extracting embedded mov_text / ass / webvtt tracks). Cue
identifiers, NOTE/STYLE/REGION blocks and cue settings are ignored safely.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass
class Cue:
    start: float  # seconds
    end: float    # seconds
    text: str


# "00:00:01,500 --> 00:00:03,500" / "00:01.500 --> 00:03.500" / with settings tail
_TS = re.compile(
    r"(?P<s>(?:\d{1,3}:)?\d{1,2}:\d{1,2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<e>(?:\d{1,3}:)?\d{1,2}:\d{1,2}[.,]\d{1,3})"
)

_TAG = re.compile(r"<[^>]+>")                    # <i>, </b>, <v Roger>, <c.cls>...
_ASS = re.compile(r"\{\\[^}]*\}")                # {\i1}, {\an8} override blocks
_VTT_INLINE_TS = re.compile(r"\d{1,2}:\d{2}[.,]\d{3}")  # inline VTT timestamps


def parse_timestamp(ts: str) -> float:
    """'HH:MM:SS,mmm' / 'MM:SS.mmm' / 'H:MM:SS.mmm' -> float seconds."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    while len(parts) < 3:
        parts.insert(0, "0")
    h, m, s = parts[-3:]
    return int(h) * 3600 + int(m) * 60 + float(s)


def clean_text(raw: str) -> str:
    """Strip markup, collapse whitespace, unescape entities, drop lead-in dashes."""
    t = _TAG.sub(" ", raw)
    t = _ASS.sub(" ", t)
    t = _VTT_INLINE_TS.sub(" ", t)
    t = html.unescape(t)
    t = t.replace("\u200b", " ").replace("\ufeff", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = t.lstrip("-\u2013\u2014\u2022 ").strip()  # leading dialogue dashes
    return t


def parse_cues(text: str) -> list:
    """Lenient SRT/WebVTT parser -> chronologically sorted list[Cue]."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        ts_idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                ts_idx = i
                break
        if ts_idx is None:
            continue  # WEBVTT header, NOTE/STYLE/REGION block, stray text...
        m = _TS.search(lines[ts_idx])
        if not m:
            continue
        start = parse_timestamp(m.group("s"))
        end = parse_timestamp(m.group("e"))
        if end <= start:
            end = start + 0.01
        body = clean_text("\n".join(lines[ts_idx + 1:]))
        if body:
            cues.append(Cue(start, end, body))
    cues.sort(key=lambda c: (c.start, c.end))
    return cues


# ---------------------------------------------------------------- naming ----

NAMING_MODES = ("HH.MM.SS.mmm", "MM.SS.mmm", "seconds.mmm")


def timestamp_name(seconds: float, mode: str = "HH.MM.SS.mmm") -> str:
    """File name from the subtitle start time.

    HH.MM.SS.mmm -> 00.00.01.500   (hour.minute.second.millisecond)
    MM.SS.mmm    -> 00.01.500
    seconds.mmm  -> 1.500
    """
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    if mode == "MM.SS.mmm":
        return f"{h * 60 + m:02d}.{s:02d}.{milli:03d}"
    if mode == "seconds.mmm":
        return f"{ms / 1000.0:.3f}"
    return f"{h:02d}.{m:02d}.{s:02d}.{milli:03d}"
