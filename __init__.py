"""ComfyUI-SubtitleAudioSlicer.

Two nodes:
- SubtitleAudioSlicer: cut a video's audio into per-subtitle FLAC + TXT dataset
  pairs using embedded or sidecar subtitles, with silence-trimmed cut points.
  Optional Demucs separation ("separation_model" input) saves the clean-voice
  file per cue instead: <timestamp>_voice.flac.
- VoiceBackgroundSeparator: extract the clean voice stem (Demucs) and save it
  as FLAC/WAV/MP3. Requires `pip install demucs`.

v1.4.0: the background stem output was removed at the user's request - in dub
workflows a background stem that drifts by even a few samples against the dub
timeline causes audible stuttering at the final merge, so both nodes now save
the voice only, and htdemucs_ft (cleaner vocals, less echo) is the default
model.

v1.5.0: the frontend browse-button layer was removed entirely at the user's
request (web/, WEB_DIRECTORY, the output-dir HTTP route). Browser-side file
picking uploads the whole video through ComfyUI's HTTP endpoint, which blocks
or stalls on multi-GB series episodes and movies. Path fields are plain text
again - type or paste an absolute path; bare file names still resolve against
ComfyUI's input/output folders (output_utils.resolve_input_path).

v1.6.0: subtitle-free sources are supported - subtitle_source = "phrase split
(no subtitles)" detects speech phrases on the track (on the Demucs voice stem
when separation is on, or on audio_path's audio), cuts one piece per phrase,
names each file by the phrase's start timestamp and writes an EMPTY .txt
placeholder per piece. Phrases longer than phrase_max_seconds (default 4.5 s)
are split at their silence gap - the split landing closest to the ideal
2.5-3 s piece length wins; pieces under 1 s are avoided unless they are a
single isolated short word.
"""

import shutil
from pathlib import Path

from .nodes import NODE_CLASS_MAPPINGS as _SLICER_MAPPINGS, \
    NODE_DISPLAY_NAME_MAPPINGS as _SLICER_NAMES
from .separator import NODE_CLASS_MAPPINGS as _SEP_MAPPINGS, \
    NODE_DISPLAY_NAME_MAPPINGS as _SEP_NAMES

NODE_CLASS_MAPPINGS = {**_SLICER_MAPPINGS, **_SEP_MAPPINGS}
NODE_DISPLAY_NAME_MAPPINGS = {**_SLICER_NAMES, **_SEP_NAMES}

__version__ = "1.6.0"


def _sas_remove_stale_web_dir() -> None:
    """Delete leftover web/ directories from pre-1.5.0 installs.

    v1.5.0 ships no frontend code at all. ComfyUI only serves a custom
    node's web directory when __init__.py defines WEB_DIRECTORY, so a stale
    web/ folder left by an in-place upgrade (unzipping the new pack over the
    old folder does not delete old files) is inert - but removing it at
    import time guarantees the browse-button layer is fully uninstalled and
    can never be half-loaded again.
    """
    stale = Path(__file__).resolve().parent / "web"
    try:
        if stale.is_dir():
            shutil.rmtree(stale)
            print("[SubtitleAudioSlicer] removed stale web/ directory from a "
                  "pre-1.5.0 install - the browse-button layer is fully "
                  "uninstalled (v1.5.0 ships no JavaScript)")
    except Exception as exc:  # never break package loading over this
        print(f"[SubtitleAudioSlicer] could not remove stale web/ directory: {exc}")


# Runs while the package is imported.
_sas_remove_stale_web_dir()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "__version__"]
