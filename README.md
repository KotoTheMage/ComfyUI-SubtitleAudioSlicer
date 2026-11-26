# ComfyUI-SubtitleAudioSlicer

Two ComfyUI custom nodes for turning subtitled video into training-ready audio:

1. **Subtitle Audio Slicer (FLAC+TXT)** — cuts a video's audio track at each
   subtitle timestamp (+/- margin, snapped to speech boundaries) and writes
   `<timestamp>.txt` + `<timestamp>.flac` dataset pairs. With the built-in
   separation enabled it instead writes the **clean-voice file per cue**:
   `<timestamp>_voice.flac`. With **no subtitles**, `phrase split` mode cuts
   the track into speech phrases (~2.5–3 s pieces) with empty `.txt`
   placeholders.
2. **Voice Separator (Demucs)** — extracts the whole track's clean **voice**
   stem (`<name>_voice.flac`) using Meta's Demucs.
3. Use https://github.com/KotoTheMage/ComfyUI-AudioTextLoader.git node to load the phrases + text line with buttons next and previous.
4. Use https://github.com/KotoTheMage/Comfyui-Dub-Merge.git node to separate the original background from voices, replace original voice with dubbed files, then merge with the background, thus, preserving the original ambience/music/etc with new voice.
> **v1.6.0 — no subtitles? phrase split.** New `subtitle_source` option
> **`phrase split (no subtitles)`**: the node detects speech phrases on the
> track (on the Demucs voice stem when separation is on, or on `audio_path`),
> cuts one piece per phrase, names each file by the phrase's **start**
> timestamp and writes an **empty `.txt`** placeholder per piece. Phrases
> longer than `phrase_max_seconds` (default 4.5 s) are split at their silence
> gap — ideal piece length 2.5–3 s, pieces under 1 s avoided unless they are
> a single short word.

> **v1.5.0 — frontend browse buttons removed.** The pick-a-file buttons are
> gone from every path field (user request): browser-side picking uploads the
> whole video through ComfyUI's HTTP endpoint, which blocks or stalls on
> multi-GB series episodes and movies. Path fields are plain text again —
> type or paste absolute paths (this always worked and remains fully
> supported). On the next start the package also deletes any stale `web/`
> folder left by an older install.

> **v1.4.0 — background output removed.** In dubbing workflows a background
> stem that drifts by even a few samples against the dub timeline causes
> audible stuttering at the final merge, so both nodes now save the **voice
> only** (user request). The `bg_suffix` widget is gone from the slicer —
> re-create the node when upgrading a saved workflow.

## One-node clean-voice output (voice per subtitle)

Set the slicer's `separation_model` to `htdemucs_ft` (default quality) or
`htdemucs` (faster) and every subtitle cue produces exactly:

```
00.00.56.264_voice.flac   <- clean voice slice
00.00.56.264.txt          <- shared transcript
```

How it works: the track is separated **once** with Demucs (not per cue), the
speech boundaries are detected on the clean **voice stem** (tightest cuts), and
each slice is cut at **exact sample boundaries**. The suffix is customisable
via `voice_suffix`.

Requires `pip install demucs` (see Installation). With `separation_model` =
`off` (default) the node behaves exactly as before: one mixed
`<timestamp>.flac` per cue.

## No subtitles? Phrase split

Set `subtitle_source` to **`phrase split (no subtitles)`** when the video (or
audio file) has no usable subtitles. The node then:

1. Runs silence detection over the track that will be cut — the Demucs **voice
   stem** when `separation_model` is on, the mixed audio otherwise, or the
   `audio_path` file when set (with `audio_path` + phrase split,
   `video_path` is not needed at all — ideal for slicing a whole-track
   `voice.flac` from the separator node).
2. Turns everything that is not silence into **phrases** (blips under 0.1 s
   are dropped as noise).
3. Keeps every phrase ≤ `phrase_max_seconds` as one piece, and splits longer
   phrases **at their silence gap** — the split whose two halves land closest
   to the ideal **2.5–3 s** piece length wins (longest gap breaks ties).
   Splits that would create a piece **under 1 s** are only used when no gap
   avoids it — the short side is then a single isolated word between pauses,
   which is exactly the allowed exception. Continuous speech with no interior
   silence is cut into equal pieces near the ideal length (a console note
   suggests raising `silence_noise_db` if that happens often).
4. Cuts each piece at its speech bounds ± `keep_silence` (sample-exact on the
   voice stem), names the files by the **phrase start time** using
   `naming_mode`, and writes an **empty `.txt`** next to each FLAC for you to
   fill in the transcription / dub line:

```
00.00.01.000.flac   <- phrase piece (voice)
00.00.01.000.txt    <- empty placeholder
00.00.03.000.flac
00.00.03.000.txt
...
```

Notes: `margin_seconds` is unused in this mode (phrase bounds already are the
speech bounds — a margin window could swallow the neighbouring phrase).
`silence_min_duration` doubles as the minimum pause that may separate two
phrases, and `phrase_max_seconds` only applies to this mode. Detection quality
depends on `silence_noise_db` — raise it (e.g. −30) for noisy tracks, lower it
(e.g. −45) for quiet speech.

## Two-node chain (alternative)

Chained together the nodes give the same clean-voice slices, but the stem is
also saved whole for reuse:

```
video.mp4 ──Voice Separator──> test_voice.flac
    │                             │
    └─ embedded subtitles         └─ audio_path ──SubtitleAudioSlicer──> 00.00.01.500.txt/.flac (clean voice)
```

## Paths: type or paste

Path fields are plain text widgets (as they were before v1.3): type or paste
absolute paths — Windows or Unix style, `~`-expanded paths, or quoted paths
(quotes are stripped). This is the fastest and most reliable option for large
media: no upload, no copying, the node reads the file exactly where it lies.

Relative names are still resolved, in this order:

1. `<ComfyUI>/input/<name>` — where ComfyUI's own upload widgets place files
2. `<ComfyUI>/output/<name>`
3. `<ComfyUI>/<name>` — covers entries like `input/clip.webm`
4. nothing matched → the node reports the exact name you typed

## Installation

1. Copy this folder into `ComfyUI/custom_nodes/`:
   ```
   ComfyUI/custom_nodes/ComfyUI-SubtitleAudioSlicer/
   ├── __init__.py
   ├── nodes.py                 # SubtitleAudioSlicer
   ├── separator.py             # VoiceBackgroundSeparator
   ├── subtitle_utils.py
   ├── ffmpeg_utils.py
   ├── output_utils.py
   ├── example_workflow.json
   └── example_workflow_separation.json
   ```
   Upgrading from 1.4.x or earlier? Just overwrite the folder — on the next
   start the package deletes the stale `web/` folder (with the old browse
   buttons) itself; watch for the `[SubtitleAudioSlicer] removed stale
   web/ directory…` line in the server console. The buttons are gone by
   design since v1.5.0 — nothing will appear under the path fields.
2. Install **ffmpeg** (the `ffmpeg` and `ffprobe` binaries must be on PATH) —
   required by both nodes:
   * Windows: `winget install ffmpeg` | macOS: `brew install ffmpeg` |
     Debian/Ubuntu: `sudo apt install ffmpeg`
3. *Only for separation (either node):* `pip install demucs` inside the
   ComfyUI python environment (torch/torchaudio are already installed by
   ComfyUI, so this adds just a few small packages). The htdemucs checkpoint
   (~80 MB) downloads automatically on first run.
4. Restart ComfyUI.

## Node 1: Subtitle Audio Slicer (FLAC+TXT)

Category `audio` · outputs `output_folder` (STRING), `segment_list` (STRING),
`segment_count` (INT).

| Input | Default | Description |
|---|---|---|
| `video_path` | — | Video file. Provides the audio (unless `audio_path` is set) and the embedded subtitles. |
| `audio_path` | empty | **Audio-source override.** When set, audio is cut/separated from this file while subtitles still come from `video_path`. |
| `separation_model` | `off` | `off` = one mixed `<timestamp>.flac` per cue. `htdemucs_ft` / `htdemucs` = separate first, then save the clean `<timestamp>_voice.flac` per cue. |
| `device` | `auto` | Separation device: `auto` = CUDA, else MPS, else CPU; can force `cuda`/`cpu`/`mps`. |
| `voice_suffix` | `_voice` | Filename suffix of the clean-voice file (separation modes only). |
| `subtitle_source` | `embedded (video)` | `embedded (video)` / `sidecar file` / `auto` / `phrase split (no subtitles)` |
| `subtitle_path` | — | Sidecar `.srt`/`.vtt`/`.ass` path (for sidecar/auto). |
| `subtitle_track` | `0` | 0-based index of the embedded subtitle track. |
| `margin_seconds` | `0.5` | Extra audio before/after each subtitle (the search window). Unused in phrase split mode. |
| `silence_noise_db` | `-35.0` | Level under which audio counts as silence. |
| `silence_min_duration` | `0.2` | Gaps shorter than this are not treated as silence. |
| `keep_silence` | `0.08` | Natural padding kept around speech bounds. |
| `phrase_max_seconds` | `4.5` | Phrase split mode: split speech longer than this at its silence gap (ideal piece 2.5–3 s, pieces <1 s avoided unless a single short word). |
| `naming_mode` | `HH.MM.SS.mmm` | `00.00.01.500` / `00.01.500` (`MM.SS.mmm`) / `1.500` (`seconds.mmm`) — from the subtitle start time. |
| `output_folder` | empty | Empty = `<ComfyUI>/output/subtitle_audio_slices/<video name>`. Any absolute or relative path. |
| `overwrite_mode` | `skip existing` | `skip existing` adds `_2`, `_3`... instead of clobbering. |

How the cut points are computed: for every cue the window
`[start − margin, end + margin]` is built, `ffmpeg silencedetect` (run once over
the whole track — over the **voice stem** when separation is on) tells where
speech actually starts/ends inside that window, the cut is placed at the speech
bounds + `keep_silence`, and segments are clamped so they never overlap. A fully
silent window falls back to the nominal margin cut.

In separation modes the voice stem is sliced at exact sample positions.
Separation runs once for the whole track, not once per cue.

## Node 2: Voice Separator (Demucs)

Category `audio` · outputs `voice_path` (STRING), `status` (STRING with
model/device/timings). (Until v1.3.6 the node also emitted a
`background_path` — removed in v1.4.0.)

| Input | Default | Description |
|---|---|---|
| `media_path` | — | Video or audio file; its first audio stream is separated. |
| `model` | `htdemucs_ft` | `htdemucs_ft` (fine-tuned ensemble of two models — cleaner vocals, far less echo, ~2× slower) or `htdemucs` (fast, good). |
| `device` | `auto` | `auto` = CUDA, else MPS, else CPU; can also force `cuda`/`cpu`/`mps`. |
| `output_format` | `flac` | `flac` (lossless) / `wav` / `mp3` (320 kbit/s). Stems are saved as 16-bit PCM. |
| `output_folder` | empty | Empty = `<ComfyUI>/output/voice/<media name>`. Any absolute or relative path. |
| `overwrite_mode` | `skip existing` | Skips clobbering; adds `_2`, `_3`... |

File: `<name>_voice.flac`. Demucs separates the whole track in one pass, so
slicing the voice stem afterwards costs nothing extra.

## Which mode should I use?

* **One node, voice file per cue** — slicer with `separation_model` set. Simplest
  graph, voice cut to exactly the subtitle segments, nothing extra on disk.
* **Two-node chain** — use when you also want the *whole-track* voice stem
  (`test_voice.flac`) for reuse, or when you slice the
  same video several times with different margins (separation then runs only
  once in the separator node).
* **Need the background back?** Install a v1.3.x zip of this pack next to the
  current one (different folder name) — v1.3.6 is the last release that wrote
  background stems.

## Example workflows

* `example_workflow.json` — single node with clean-voice output (`_voice` per cue).
* `example_workflow_separation.json` — two-node chain with the whole-track voice stem.

## Troubleshooting

* **Where did the browse buttons go?** Removed in v1.5.0 (user request):
  browser-side picking uploads the whole file through ComfyUI's HTTP
  endpoint, which blocks or stalls on multi-GB series episodes and movies.
  Type or paste paths instead — see *Paths: type or paste* above. If a
  stale `web/` folder still exists from an older install, the package
  deletes it automatically on the next start.
* **"no embedded subtitle track"** — the video carries no text subtitles. Use a
  sidecar file. Check tracks with:
  `ffprobe -v error -show_entries stream=index,codec_type,codec_name:stream_tags=language -of compact video.mp4`
* **"yielded no text cues"** — the selected track is bitmap-based (PGS/VobSub);
  use another track or OCR first.
* **"demucs is not installed"** (either node, separation enabled) — run
  `pip install demucs` in the ComfyUI python env and restart.
* **Separator: corrupted checkpoint** (`BadZipFile` / download interrupted) —
  delete `~/.cache/torch/hub/checkpoints/*.th` (Windows:
  `%USERPROFILE%\.cache\torch\hub\checkpoints\*.th`) and run again.
* **Separation is slow on CPU** — expect ~0.2–0.5× realtime (htdemucs; about
  half that for `htdemucs_ft`); on an NVIDIA GPU it is 10–50× faster. It runs
  once per track, not per cue, in both nodes.
* **The voice has a slight echo / reverb character** — that is the separation
  model's inherent artifact, not a processing bug: the pack's separation is
  verified sample-aligned with the official `demucs` CLI (the automated test
  cross-correlates both outputs; peak at lag 0, r ≈ 0.998). It becomes audible
  once the voice is soloed for dubbing. Mitigations, best first:
  1. Use **`htdemucs_ft`** (the default since v1.4.0) — noticeably cleaner
     vocals than plain `htdemucs`.
  2. Prefer lossless output (`flac`) end-to-end; each lossy re-encode adds a
     little smear.
  3. If you must squeeze further, higher `--shifts` values in the official CLI
     help marginally but cost several times the compute.
* **Dub merge drifts against the video** — cut the dubs from the same source
  timeline: the slicer's cuts are sample-exact on the decoded audio timeline;
  container-level offsets (AAC priming etc.) affect every tool equally, so
  align dubs to the decoded audio, not to the container timestamps.
* **Quiet speech trimmed away by the slicer** — lower `silence_noise_db`
  (e.g. −45) or `silence_min_duration`.

## Notes

* Both nodes process long files in one pass (audio extracted once, silence
  detected once, separation once); temp files are cleaned up automatically.
  Separation holds the whole-track stems in memory, so very long videos need
  a few GB of RAM on CPU/GPU.
* Windows paths with backslashes are fine; pasted quotes are stripped; `~`
  expands; relative names resolve against ComfyUI's input/output folders
  (see *Paths: type or paste* above).
* Version: 1.6.0 (new `subtitle_source` mode **phrase split (no subtitles)**:
  speech phrases are detected on the track — the Demucs voice stem when
  separation is on, or the `audio_path` file, which no longer requires
  `video_path` — and one piece is cut per phrase, named by the phrase start
  time with an empty `.txt` placeholder; phrases longer than
  `phrase_max_seconds` (default 4.5 s) are split at their silence gap, the
  split landing closest to the ideal 2.5–3 s piece length wins, pieces under
  1 s are avoided unless they are a single isolated short word; blips under
  0.1 s are dropped as noise. 1.5.0 removed the frontend browse-button layer
  entirely (browser-side picking uploads whole videos through ComfyUI's HTTP
  endpoint, which blocks or stalls on multi-GB files; `web/`, `WEB_DIRECTORY`
  and the output-folder HTTP route are gone, path fields are plain text
  again, relative-name resolution kept). 1.4.0 removed the background output
  at the user's request — in dub workflows a background stem that drifts by
  even a few samples against the
  dub timeline causes audible stuttering at the final merge, so the separator
  saves `<name>_voice.flac` only and the slicer's separation mode writes one
  `<timestamp>_voice.flac` + `<timestamp>.txt` per cue; the `bg_suffix`
  widget is gone, so re-create the slicer node when upgrading a saved
  workflow; `htdemucs_ft` is now the default model — fine-tuned, cleaner
  vocals with less echo; the separation call pins the demucs-CLI defaults
  (`shifts=1`, `overlap=0.25`) explicitly and is test-verified to be
  sample-aligned with the official CLI (peak cross-correlation at lag 0);
  flexible path resolution).
* License: MIT.
