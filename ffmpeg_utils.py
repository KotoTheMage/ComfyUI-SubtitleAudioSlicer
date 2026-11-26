"""Thin ffmpeg/ffprobe wrappers used by the SubtitleAudioSlicer node.

Only the ffmpeg *binaries* are required (no pip packages). Every helper
raises FFmpegError with a short, user-friendly message when something fails.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class FFmpegError(RuntimeError):
    pass


def require_binaries():
    missing = [n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)) if not p]
    if missing:
        raise FFmpegError(
            f"[SubtitleAudioSlicer] {', '.join(missing)} not found on PATH. "
            "Install ffmpeg and restart ComfyUI. "
            "Windows: winget install ffmpeg | macOS: brew install ffmpeg | "
            "Debian/Ubuntu: sudo apt install ffmpeg"
        )


def _run(cmd, timeout=None):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = "\n".join(err[-6:]) if err else "(no stderr)"
        raise FFmpegError(
            f"[SubtitleAudioSlicer] command failed:\n  {' '.join(map(str, cmd))}\n{tail}"
        )
    return proc


def probe_streams(path):
    """Return parsed ffprobe JSON (streams + format) for a media file."""
    proc = _run([FFPROBE, "-v", "error", "-show_streams", "-show_format",
                 "-of", "json", str(path)], timeout=120)
    return json.loads(proc.stdout.decode("utf-8", "replace"))


def probe_duration(path) -> float:
    info = probe_streams(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        for s in info.get("streams", []):
            if "duration" in s:
                return float(s["duration"])
        raise FFmpegError(f"[SubtitleAudioSlicer] cannot determine duration of {path}")


def probe_has_audio(path) -> bool:
    info = probe_streams(path)
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def probe_subtitle_tracks(path):
    """List subtitle tracks: [{rel_index, stream_index, codec, language, title}]."""
    info = probe_streams(path)
    tracks = []
    for s in info.get("streams", []):
        if s.get("codec_type") == "subtitle":
            tags = s.get("tags") or {}
            tracks.append({
                "rel_index": len(tracks),
                "stream_index": s.get("index"),
                "codec": s.get("codec_name", "?"),
                "language": tags.get("language", "?"),
                "title": tags.get("title", ""),
            })
    return tracks


def extract_subtitle_to_srt(video_path, track, out_path):
    """Extract embedded subtitle track `track` (0-based among subs) to SRT.

    ffmpeg converts mov_text / ass / webvtt / subrip -> srt on the fly.
    Bitmap formats (PGS, VobSub) fail with a clear error.
    """
    _run([FFMPEG, "-y", "-v", "error", "-i", str(video_path),
          "-map", f"0:s:{int(track)}", "-f", "srt", str(out_path)], timeout=300)
    return out_path


def convert_subtitle_to_srt(sub_path, out_path):
    """Convert a sidecar subtitle file (e.g. .ass/.ssa/.vtt) to SRT."""
    _run([FFMPEG, "-y", "-v", "error", "-i", str(sub_path),
          "-f", "srt", str(out_path)], timeout=300)
    return out_path


def extract_audio_to_wav(video_path, out_path):
    """Extract the first audio stream as 16-bit PCM wav (sample-accurate cuts)."""
    _run([FFMPEG, "-y", "-v", "error", "-i", str(video_path),
          "-vn", "-sn", "-dn", "-map", "0:a:0", "-acodec", "pcm_s16le",
          str(out_path)], timeout=3600)
    return out_path


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)")


def detect_silence(wav_path, noise_db, min_dur):
    """Return silence intervals [(start, end)] in seconds (end may be inf)."""
    proc = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(wav_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
         "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    log = proc.stderr.decode("utf-8", "replace")
    silences, cur = [], None
    for line in log.splitlines():
        m = _SILENCE_START.search(line)
        if m:
            cur = float(m.group(1))
            continue
        m = _SILENCE_END.search(line)
        if m and cur is not None:
            silences.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:  # silence until EOF
        silences.append((cur, float("inf")))
    return silences


def cut_flac(src, start, end, out_path):
    """Cut [start, end) seconds of `src` into a FLAC file (accurate seek)."""
    dur = max(0.0, end - start)
    _run([FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
          "-t", f"{dur:.3f}", "-vn", "-acodec", "flac", str(out_path)],
         timeout=600)
    return out_path


def write_pcm16(pcm_bytes, sample_rate, channels, out_path, fmt="flac"):
    """Encode raw little-endian interleaved s16 PCM (piped via stdin) to flac/wav/mp3.

    Keeps stem saving independent of torchaudio's save backends - only numpy
    (already a demucs dependency) and ffmpeg are involved.
    """
    codec = {"flac": ["-acodec", "flac"],
             "wav": ["-acodec", "pcm_s16le"],
             "mp3": ["-acodec", "libmp3lame", "-b:a", "320k"]}.get(fmt)
    if codec is None:
        raise FFmpegError(f"[SubtitleAudioSlicer] unsupported output format: {fmt}")
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "s16le", "-ar", str(int(sample_rate)),
           "-ac", str(int(channels)), "-i", "pipe:0", *codec, str(out_path)]
    proc = subprocess.run(cmd, input=pcm_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=1800)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = "\n".join(err[-6:]) if err else "(no stderr)"
        raise FFmpegError(f"[SubtitleAudioSlicer] PCM encode failed:\n{tail}")
    return out_path
