"""SubtitleAudioSlicer — a ComfyUI custom node.

Loads the audio track of a video (.webm / .mp4 / .mkv / ...), reads subtitles
from an embedded track or a sidecar file, cuts the audio per subtitle cue
(+/- margin, refined to speech boundaries via ffmpeg silencedetect) and
writes `<timestamp>.txt` + `<timestamp>.flac` pairs — a ready-made
TTS / ASR dataset folder.

With `separation_model` set to a Demucs model, the track is split into voice
and background first and every cue produces the clean-voice file cut at exact
sample boundaries — `<timestamp>_voice.flac` — plus the shared
`<timestamp>.txt`. Speech boundaries are detected on the clean voice stem,
which gives the tightest cuts. (v1.4.0 dropped the per-cue `_bg` file on user
request: in dubbing merges a background stem that drifts by even a few
samples causes audible stuttering.)

v1.6.0 adds subtitle-free sources: subtitle_source = "phrase split (no
subtitles)" detects speech phrases on the track (on the voice stem when
separation is on), cuts one piece per phrase and writes an EMPTY
`<timestamp>.txt` placeholder per piece. A phrase longer than
`phrase_max_seconds` (default 4.5 s) is split at its silence gap - the gap
whose halves land closest to the ideal 2.5-3 s length wins; pieces under
1 s are avoided unless they are a single isolated short word.
"""

from __future__ import annotations

import math
import os
import tempfile

from . import ffmpeg_utils as ff
from .output_utils import resolve_input_path, resolve_output_dir, unique_name
from .separator import DEVICES, MODELS, encode_stem, separate_to_tensors
from .subtitle_utils import NAMING_MODES, Cue, parse_cues, timestamp_name

try:
    from comfy.utils import ProgressBar
except Exception:  # standalone use / tests
    ProgressBar = None

MIN_CUT = 0.02  # seconds; discard absurdly short cuts

# ---- phrase split mode (subtitle_source == PHRASE_SOURCE) ----
PHRASE_SOURCE = "phrase split (no subtitles)"
PHRASE_MAX_DEFAULT = 4.5  # split speech longer than this many seconds
PHRASE_TARGET = 2.75      # ideal piece duration (middle of the 2.5-3 s band)
PHRASE_MIN = 1.0          # never CREATE a piece shorter than this when splitting
PHRASE_BLIP = 0.10        # speech regions shorter than this are noise, dropped


def _clean_suffix(value: str, default: str) -> str:
    """Normalise a filename suffix typed by the user (default '_voice')."""
    s = (value or "").strip().strip('"').strip("'")
    if not s:
        s = default
    if s in (".", "..") or "/" in s or "\\" in s:
        raise ValueError(
            f"[SubtitleAudioSlicer] invalid file suffix: {value!r} "
            "(must not contain path separators)"
        )
    return s


def _speech_regions(silences, duration):
    """Complement of the silence map -> [(start, end)] speech regions."""
    regions, pos = [], 0.0
    for s0, s1 in silences:
        s0 = max(0.0, s0)
        s1 = duration if s1 == float("inf") else min(s1, duration)
        if s0 > pos + 1e-6:
            regions.append((pos, s0))
        pos = max(pos, s1)
    if pos < duration - 1e-6:
        regions.append((pos, duration))
    return [r for r in regions if r[1] - r[0] >= PHRASE_BLIP]


def _split_long_region(s0, s1, silences, max_len,
                       tag="[SubtitleAudioSlicer]"):
    """Split the speech region (s0, s1) at silence gaps while it is too long.

    Rule: a region longer than `max_len` is split at an interior silence gap -
    the gap whose two halves land closest to the ideal PHRASE_TARGET length
    wins (longest gap breaks ties). Splits creating a piece shorter than
    PHRASE_MIN are only used when no gap avoids it (the short side is then a
    single isolated word between pauses, which is acceptable). Continuous
    speech with no interior silence is cut into equal pieces as close to the
    ideal length as possible.
    """
    dur = s1 - s0
    if dur <= max_len:
        return [(s0, s1)]
    gaps = [(a, b) for a, b in silences if a > s0 + 1e-6 and b < s1 - 1e-6]
    if gaps:
        valid = [g for g in gaps
                 if g[0] - s0 >= PHRASE_MIN - 1e-9
                 and s1 - g[1] >= PHRASE_MIN - 1e-9]
        if valid:
            best = min(valid, key=lambda g: (
                abs(g[0] - s0 - PHRASE_TARGET) + abs(s1 - g[1] - PHRASE_TARGET),
                -(g[1] - g[0])))
        else:
            # every gap leaves a <1 s piece on one side - take the most
            # balanced gap; the short side becomes an isolated short word
            best = max(gaps, key=lambda g: min(g[0] - s0, s1 - g[1]))
        return (_split_long_region(s0, best[0], silences, max_len, tag) +
                _split_long_region(best[1], s1, silences, max_len, tag))
    n = max(2, math.ceil(dur / max_len), round(dur / PHRASE_TARGET))
    print(f"{tag} {dur:.2f}s of continuous speech with no interior silence "
          f"gap - cut into {n} equal pieces (raise silence_noise_db if this "
          "happens too often)")
    step = dur / n
    return [(s0 + i * step, s0 + (i + 1) * step) for i in range(n)]


def _phrase_cues(silences, duration, max_len,
                 tag="[SubtitleAudioSlicer]"):
    """Silence map -> chronologically sorted list[Cue] with empty text."""
    cues = []
    for a, b in _speech_regions(silences, duration):
        for a2, b2 in _split_long_region(a, b, silences, max_len, tag):
            cues.append(Cue(a2, b2, ""))
    cues.sort(key=lambda c: c.start)
    return cues


def _exists_fn(out_dir, dual, voice_suffix):
    """Collision probe for unique_name: the segment's voice flac or txt existing."""
    if dual:
        return lambda c: (
            os.path.exists(os.path.join(out_dir, f"{c}{voice_suffix}.flac")) or
            os.path.exists(os.path.join(out_dir, c + ".txt"))
        )
    return lambda c: (
        os.path.exists(os.path.join(out_dir, c + ".flac")) or
        os.path.exists(os.path.join(out_dir, c + ".txt"))
    )


class SubtitleAudioSlicer:
    """Cut video audio into per-subtitle FLAC + TXT dataset pairs.

    Optional Demucs separation writes a clean `_voice` FLAC per segment.
    Without subtitles, 'phrase split' mode cuts one piece per detected
    speech phrase and writes empty .txt placeholders.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_path": ("STRING", {
                    "default": "", "multiline": False,
                    "placeholder": "e.g. D:/clips/interview.webm",
                    "tooltip": "Path to the video file (.webm, .mp4, .mkv, ...). "
                               "Provides the audio (unless audio_path is set) and the "
                               "embedded subtitles. Type or paste an absolute path; a "
                               "bare file name is also resolved inside ComfyUI's input "
                               "and output folders.",
                }),
                "audio_path": ("STRING", {
                    "default": "", "multiline": False,
                    "placeholder": "e.g. voice.flac from the separator node",
                    "tooltip": "Optional audio-source override, e.g. voice.flac from "
                               "VoiceBackgroundSeparator. When set, audio is cut from "
                               "this file while subtitles still come from video_path. "
                               "Absolute path, or a name resolved inside ComfyUI's "
                               "input/output folders.",
                }),
                "separation_model": (["off", *MODELS], {
                    "tooltip": "off: one mixed <timestamp>.flac per cue. "
                               "htdemucs_ft / htdemucs: separate the track with "
                               "Demucs first, then save the clean-voice file "
                               "<timestamp>_voice.flac per cue (requires "
                               "`pip install demucs`).",
                }),
                "device": (list(DEVICES), {
                    "tooltip": "Separation device. auto: CUDA if available, "
                               "then MPS, else CPU.",
                }),
                "voice_suffix": ("STRING", {
                    "default": "_voice", "multiline": False,
                    "tooltip": "Filename suffix for the clean-voice file "
                               "(separation modes only).",
                }),
                "subtitle_source": (["embedded (video)", "sidecar file", "auto",
                                     PHRASE_SOURCE], {
                    "tooltip": "embedded: read a subtitle track stored inside the video. "
                               "sidecar: read the file given in subtitle_path. "
                               "auto: sidecar if provided, otherwise embedded. "
                               "phrase split (no subtitles): no subtitles are "
                               "read - speech phrases are detected on the track "
                               "and one piece is cut per phrase, with an EMPTY "
                               ".txt placeholder per piece.",
                }),
                "subtitle_path": ("STRING", {
                    "default": "", "multiline": False,
                    "placeholder": "e.g. D:/subs/interview.srt",
                    "tooltip": "Sidecar subtitle file (.srt / .vtt / .ass / .ssa). "
                               "Used with 'sidecar file' or 'auto'. Absolute path, "
                               "or a name resolved inside ComfyUI's input/output "
                               "folders.",
                }),
                "subtitle_track": ("INT", {
                    "default": 0, "min": 0, "max": 63, "step": 1,
                    "tooltip": "0-based index of the embedded subtitle track "
                               "(only for 'embedded (video)').",
                }),
                "margin_seconds": ("FLOAT", {
                    "default": 0.5, "min": 0.0, "max": 10.0, "step": 0.05,
                    "tooltip": "Extra audio added before/after each subtitle. "
                               "Silence trimming then snaps the cut to speech edges "
                               "inside this window. (Unused in phrase split mode.)",
                }),
                "silence_noise_db": ("FLOAT", {
                    "default": -35.0, "min": -90.0, "max": 0.0, "step": 1.0,
                    "tooltip": "Audio below this level counts as silence when "
                               "refining cut points. Raise it (e.g. -30) for noisy "
                               "recordings, lower it (e.g. -45) to keep quiet speech.",
                }),
                "silence_min_duration": ("FLOAT", {
                    "default": 0.2, "min": 0.01, "max": 5.0, "step": 0.01,
                    "tooltip": "Gaps shorter than this are not treated as silence.",
                }),
                "keep_silence": ("FLOAT", {
                    "default": 0.08, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "Silence kept around the speech bounds after trimming "
                               "so cuts do not sound clipped.",
                }),
                "phrase_max_seconds": ("FLOAT", {
                    "default": 4.5, "min": 1.0, "max": 30.0, "step": 0.1,
                    "tooltip": "'phrase split' mode only: speech phrases longer than "
                               "this are split at their silence gap - the split "
                               "that lands closest to the ideal 2.5-3 s piece "
                               "length wins, and pieces under 1 s are avoided "
                               "unless they are a single short word.",
                }),
                "naming_mode": (list(NAMING_MODES), {
                    "tooltip": "File name format, derived from each subtitle's start "
                               "time. HH.MM.SS.mmm -> 00.00.01.500",
                }),
                "output_folder": ("STRING", {
                    "default": "", "multiline": False,
                    "placeholder": "empty = auto: output/subtitle_audio_slices/<video>",
                    "tooltip": "Destination folder. Empty = "
                               "<ComfyUI>/output/subtitle_audio_slices/<video name>. "
                               "Type any absolute or relative path.",
                }),
                "overwrite_mode": (["skip existing", "overwrite"], {
                    "tooltip": "What to do when a file with the same timestamp name "
                               "already exists.",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "INT")
    RETURN_NAMES = ("output_folder", "segment_list", "segment_count")
    OUTPUT_NODE = True
    FUNCTION = "process"
    CATEGORY = "audio"

    # ------------------------------------------------------------- helpers --

    def _load_cues(self, video_path, subtitle_source, subtitle_path,
                   subtitle_track, tmp):
        """Return list[Cue] according to the chosen subtitle source."""
        sub_path = resolve_input_path(subtitle_path)

        if subtitle_source == "sidecar file" and not (sub_path and os.path.isfile(sub_path)):
            raise ValueError(
                f"[SubtitleAudioSlicer] sidecar subtitle not found: {sub_path or '(empty)'}"
            )
        use_sidecar = subtitle_source == "sidecar file" or (
            subtitle_source == "auto" and sub_path and os.path.isfile(sub_path)
        )
        if subtitle_source == "auto" and sub_path and not os.path.isfile(sub_path):
            print(f"[SubtitleAudioSlicer] sidecar '{sub_path}' not found, "
                  f"falling back to embedded tracks.")

        if use_sidecar:
            ext = os.path.splitext(sub_path)[1].lower()
            if ext in (".ass", ".ssa"):
                conv = os.path.join(tmp, "sidecar.srt")
                ff.convert_subtitle_to_srt(sub_path, conv)
                sub_path = conv
            with open(sub_path, "r", encoding="utf-8", errors="replace") as f:
                cues = parse_cues(f.read())
            if not cues:
                raise ValueError(f"[SubtitleAudioSlicer] no cues parsed from {sub_path}")
            return cues

        # ---- embedded tracks ----
        tracks = ff.probe_subtitle_tracks(video_path)
        if not tracks:
            raise ValueError(
                f"[SubtitleAudioSlicer] no embedded subtitle track in {video_path}. "
                "Provide a sidecar subtitle via subtitle_path or switch "
                "subtitle_source to 'sidecar file'."
            )
        if subtitle_track >= len(tracks):
            listing = "\n".join(
                f"  track {t['rel_index']}: {t['codec']} [{t['language']}] {t['title']}".rstrip()
                for t in tracks
            )
            raise ValueError(
                f"[SubtitleAudioSlicer] subtitle_track {subtitle_track} does not "
                f"exist. Available tracks:\n{listing}"
            )
        srt = os.path.join(tmp, f"embedded_{subtitle_track}.srt")
        ff.extract_subtitle_to_srt(video_path, subtitle_track, srt)
        with open(srt, "r", encoding="utf-8", errors="replace") as f:
            cues = parse_cues(f.read())
        if not cues:
            raise ValueError(
                f"[SubtitleAudioSlicer] embedded track {subtitle_track} yielded no "
                "text cues. Bitmap subtitles (PGS / VobSub) cannot be converted to "
                "text - use a text track or a sidecar file."
            )
        return cues

    @staticmethod
    def _speech_bounds(silences, duration, w0, w1):
        """First speech start / last speech end inside [w0, w1] (or None)."""
        speech = []
        pos = 0.0
        for s0, s1 in silences:
            s0 = max(0.0, s0)
            s1 = duration if s1 == float("inf") else min(s1, duration)
            if s0 > pos:
                speech.append((pos, s0))
            pos = max(pos, s1)
        if pos < duration:
            speech.append((pos, duration))
        first = last = None
        for a, b in speech:
            a2, b2 = max(a, w0), min(b, w1)
            if a2 < b2 - 1e-6:
                if first is None:
                    first = a2
                last = b2
        if first is None:
            return None
        return first, last

    # ---------------------------------------------------------------- run ----

    def process(self, video_path, subtitle_source, subtitle_path, subtitle_track,
                margin_seconds, silence_noise_db, silence_min_duration,
                keep_silence, naming_mode, output_folder, overwrite_mode,
                audio_path="", separation_model="off", device="auto",
                voice_suffix="_voice", phrase_max_seconds=PHRASE_MAX_DEFAULT):
        ff.require_binaries()

        dual = separation_model != "off"
        phrase_mode = subtitle_source == PHRASE_SOURCE
        voice_suffix = _clean_suffix(voice_suffix, "_voice")

        video_path = resolve_input_path(video_path)
        audio_src = resolve_input_path(audio_path)
        if audio_src and not os.path.isfile(audio_src):
            raise ValueError(
                f"[SubtitleAudioSlicer] audio override not found: {audio_src}"
            )
        if not audio_src:
            if not video_path or not os.path.isfile(video_path):
                raise ValueError(
                    f"[SubtitleAudioSlicer] video not found: {video_path or '(empty)'}"
                )
            audio_file = video_path
        else:
            audio_file = audio_src
            # with an audio override the video is only needed as subtitle source
            sub_file = resolve_input_path(subtitle_path)
            needs_video = (
                not phrase_mode
                and (subtitle_source == "embedded (video)"
                     or (subtitle_source == "auto"
                         and not (sub_file and os.path.isfile(sub_file))))
            )
            if needs_video and not (video_path and os.path.isfile(video_path)):
                raise ValueError(
                    "[SubtitleAudioSlicer] audio_path overrides the audio, so "
                    "video_path is only needed for subtitles - but subtitle_source "
                    f"is '{subtitle_source}' and no valid sidecar subtitle was given."
                )

        out_dir = resolve_output_dir(
            output_folder, "subtitle_audio_slices",
            os.path.splitext(os.path.basename(video_path or audio_src))[0])

        with tempfile.TemporaryDirectory(prefix="saslicer_") as tmp:
            if not ff.probe_has_audio(audio_file):
                raise ValueError(f"[SubtitleAudioSlicer] no audio stream in {audio_file}")

            # fail fast on subtitle problems BEFORE any expensive separation
            cues = None if phrase_mode else self._load_cues(
                video_path, subtitle_source, subtitle_path, subtitle_track, tmp)

            if dual:
                print(f"[SubtitleAudioSlicer] separating the clean voice stem of "
                      f"the whole track ({separation_model}) - runs once, not per cue ...")
                voice_t, stem_sr, stem_dur, _dev = separate_to_tensors(
                    audio_file, separation_model, device,
                    log_tag="[SubtitleAudioSlicer]")
                n_samples = voice_t.shape[-1]
                duration = stem_dur
                # silence analysis on the clean voice stem -> best speech bounds
                analysis = os.path.join(tmp, "voice_stem.flac")
                encode_stem(voice_t, stem_sr, analysis, "flac")
            else:
                duration = ff.probe_duration(audio_file)
                analysis = os.path.join(tmp, "audio.wav")
                ff.extract_audio_to_wav(audio_file, analysis)

            silences = ff.detect_silence(analysis, silence_noise_db,
                                         silence_min_duration)

            if phrase_mode:
                if (subtitle_path or "").strip():
                    print("[SubtitleAudioSlicer] phrase split mode: "
                          "subtitle_path / subtitle_track are ignored")
                if not dual:
                    print("[SubtitleAudioSlicer] phrase split runs on the "
                          "mixed audio - set separation_model to a Demucs "
                          "model for cleaner phrase detection")
                cues = _phrase_cues(silences, duration, phrase_max_seconds)
                if not cues:
                    raise ValueError(
                        "[SubtitleAudioSlicer] phrase split found no speech on "
                        f"the track ({audio_file}). Adjust silence_noise_db "
                        "(raise it, e.g. -30, for noisy tracks; lower it, e.g. "
                        "-45, for quiet speech), check silence_min_duration, "
                        "or verify the file actually contains speech."
                    )
                print(f"[SubtitleAudioSlicer] phrase split: {len(cues)} phrase "
                      f"piece(s) detected (split above {phrase_max_seconds:.1f}s, "
                      f"ideal ~{PHRASE_TARGET:.2f}s)")

            pbar = ProgressBar(len(cues)) if ProgressBar else None
            results, skipped = [], []
            prev_end, used = 0.0, set()

            for cue in cues:
                if phrase_mode:
                    # cue bounds ARE the speech bounds - pad with keep_silence
                    # only; a margin window here could swallow the neighbouring
                    # phrase when the gap between phrases is small.
                    start = max(0.0, cue.start - keep_silence)
                    end = min(duration, cue.end + keep_silence)
                else:
                    w0 = max(0.0, cue.start - margin_seconds)
                    w1 = min(duration, cue.end + margin_seconds)
                    if w1 - w0 < MIN_CUT:
                        skipped.append((cue, "window too small"))
                        if pbar:
                            pbar.update(1)
                        continue

                    bounds = self._speech_bounds(silences, duration, w0, w1)
                    if bounds:
                        start = max(w0, bounds[0] - keep_silence)
                        end = min(w1, bounds[1] + keep_silence)
                    else:  # fully silent window -> keep the nominal margin cut
                        start, end = w0, w1

                start = max(start, prev_end)  # never overlap previous segment
                if end - start < MIN_CUT:
                    skipped.append((cue, "no free audio left in window"))
                    if pbar:
                        pbar.update(1)
                    continue
                prev_end = end

                name = unique_name(
                    out_dir, timestamp_name(cue.start, naming_mode),
                    _exists_fn(out_dir, dual, voice_suffix),
                    overwrite_mode, used)
                txt_path = os.path.join(out_dir, name + ".txt")

                if dual:
                    # exact sample boundaries -> cuts sit on the decoded timeline
                    i0 = max(0, int(round(start * stem_sr)))
                    i1 = min(n_samples, int(round(end * stem_sr)))
                    encode_stem(voice_t[:, i0:i1], stem_sr,
                                os.path.join(out_dir, f"{name}{voice_suffix}.flac"),
                                "flac")
                else:
                    ff.cut_flac(analysis, start, end,
                                os.path.join(out_dir, name + ".flac"))
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write((cue.text + "\n") if cue.text else "")

                results.append((name, start, end, cue.text))
                if pbar:
                    pbar.update(1)

        if dual:
            lines = [f"{n}{voice_suffix}.flac | "
                     f"{s:9.3f} - {e:9.3f} | {t}"
                     for n, s, e, t in results]
        else:
            lines = [f"{n} | {s:9.3f} - {e:9.3f} | {t}"
                     for n, s, e, t in results]
        segment_list = "\n".join(lines) if lines else "(no segments written)"
        msg = f"[SubtitleAudioSlicer] wrote {len(results)} segment(s) to {out_dir}"
        if dual:
            msg += " (clean voice)"
        if phrase_mode:
            msg += " (phrase split, empty .txt placeholders)"
        if skipped:
            msg += f", skipped {len(skipped)} cue(s)"
        print(msg)

        return (out_dir, segment_list, len(results))


NODE_CLASS_MAPPINGS = {"SubtitleAudioSlicer": SubtitleAudioSlicer}
NODE_DISPLAY_NAME_MAPPINGS = {"SubtitleAudioSlicer": "Subtitle Audio Slicer (FLAC+TXT)"}
