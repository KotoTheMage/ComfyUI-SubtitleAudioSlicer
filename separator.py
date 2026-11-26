"""VoiceBackgroundSeparator — ComfyUI custom node.

Extracts a clean **voice** stem from the audio track of a video (or any audio
file) using Meta's Demucs models and saves it as FLAC / WAV / MP3:

    <output_folder>/<name>_voice.flac

v1.4.0 removed the background output on user request: in dubbing workflows a
background stem whose timeline drifts by even a few samples causes audible
stuttering at the final merge, so this node now saves the voice only.

The separation core (`separate_to_tensors`) is exposed at module level so the
SubtitleAudioSlicer node can reuse it for its built-in clean-voice
per-segment output mode. Demucs is imported lazily inside that function, so
the package still loads (and both nodes register) on machines where demucs is
not installed.
"""

from __future__ import annotations

import os
import time

from . import ffmpeg_utils as ff
from .output_utils import resolve_input_path, resolve_output_dir, unique_name

# htdemucs_ft first = dropdown default: fine-tuned, ensemble of two models,
# noticeably cleaner vocals (less echo) at ~2x the cost. htdemucs stays
# available as the fast option.
MODELS = ("htdemucs_ft", "htdemucs")
DEVICES = ("auto", "cuda", "cpu", "mps")
FORMATS = ("flac", "wav", "mp3")

_MODEL_CACHE = {}  # (model_name, device) -> loaded model


# ------------------------------------------------------- separation core ----

def pick_device(pref: str) -> str:
    """Resolve a device preference ('auto'/'cuda'/'cpu'/'mps') to a real one."""
    import torch

    if pref == "cuda":
        if not torch.cuda.is_available():
            raise ValueError(
                "[VoiceBackgroundSeparator] device 'cuda' requested but no CUDA "
                "GPU is available. Install a CUDA build of torch or use 'auto'."
            )
        return "cuda"
    if pref == "mps":
        mps = getattr(torch.backends, "mps", None)
        if not (mps and torch.backends.mps.is_available()):
            raise ValueError(
                "[VoiceBackgroundSeparator] device 'mps' requested but Apple "
                "Metal/MPS is not available on this machine."
            )
        return "mps"
    if pref == "cpu":
        return "cpu"
    # auto
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def get_model(name: str, device: str):
    """Load (and cache) a Demucs model on the given device."""
    from demucs.pretrained import get_model as _load

    key = (name, device)
    if key not in _MODEL_CACHE:
        model = _load(name)
        model.to(device)
        _MODEL_CACHE[key] = model
    return _MODEL_CACHE[key]


def encode_stem(tensor, sample_rate: int, out_path: str, fmt: str = "flac"):
    """float tensor (channels, samples) -> 16-bit flac/wav/mp3 via ffmpeg.

    Works on any tensor slice; never mutates the input (out-of-place clamp).
    """
    import numpy as np
    import torch

    data = torch.clamp(tensor.detach().to("cpu").float(), -1.0, 1.0).numpy()
    pcm = np.ascontiguousarray(data.T)                          # (n, ch)
    pcm = np.clip((pcm * 32767.0), -32768, 32767).astype("<i2")
    ff.write_pcm16(pcm.tobytes(), sample_rate, pcm.shape[1], out_path, fmt)
    return out_path


def separate_to_tensors(media_path, model_name, device_pref="auto",
                        log_tag="[VoiceBackgroundSeparator]"):
    """Demucs-separate a media file's first audio stream into a voice stem.

    Returns (voice, sample_rate, duration, device) where voice is a float
    tensor shaped (channels, samples) on the decoded-audio timeline.
    Raises ImportError with install instructions when demucs is missing.
    """
    try:
        import torch
        from demucs.apply import apply_model
        from demucs.audio import AudioFile
    except ImportError as e:
        raise ImportError(
            f"{log_tag} demucs is not installed. Run `pip install demucs` inside "
            "the ComfyUI python environment (torch is already there), then "
            "restart ComfyUI. See requirements.txt."
        ) from e

    dev = pick_device(device_pref)
    demucs_model = get_model(model_name, dev)
    sr = demucs_model.samplerate
    if "vocals" not in demucs_model.sources:
        raise ValueError(
            f"{log_tag} model {model_name} has no 'vocals' stem: "
            f"{demucs_model.sources}"
        )

    print(f"{log_tag} decoding audio (device={dev}, model={model_name}) ...")
    wav = AudioFile(media_path).read(streams=0, samplerate=sr,
                                     channels=demucs_model.audio_channels)
    duration = wav.shape[-1] / sr

    # same input normalisation as the demucs CLI / api.Separator
    # (the +1e-8 keeps digital-silence input from producing NaNs)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    wav = (wav - mean) / std

    print(f"{log_tag} separating {duration:.1f}s of audio on {dev} "
          "(CPU: expect roughly 0.2-0.5x realtime) ...")
    with torch.no_grad():
        # shifts=1 matches the demucs CLI default: the prediction is repeated
        # with a random sub-second shift and averaged, which audibly reduces
        # echo-type separation artifacts in the voice stem.
        sources = apply_model(demucs_model, wav[None], device=dev, shifts=1,
                              split=True, overlap=0.25,
                              progress=False)[0]        # (stems, ch, n)
    sources = sources * std + mean

    vocals = demucs_model.sources.index("vocals")
    voice = sources[vocals]
    del sources  # free the full stem stack early (long tracks are RAM-hungry)

    return voice, sr, duration, dev


# ----------------------------------------------------------------- node ----

class VoiceBackgroundSeparator:
    """Extract the clean voice stem (Demucs) and save it - voice only."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "media_path": ("STRING", {
                    "default": "", "multiline": False,
                    "placeholder": "e.g. D:/clips/interview.mp4",
                    "tooltip": "Video (.webm/.mp4/.mkv/...) or audio file whose first "
                               "audio stream is separated. Type or paste an absolute "
                               "path; a bare file name is also resolved inside "
                               "ComfyUI's input and output folders.",
                }),
                "model": (list(MODELS), {
                    "tooltip": "htdemucs_ft (default): fine-tuned ensemble of two "
                               "models - cleaner vocals with far less echo, ~2x "
                               "slower. htdemucs: faster, slightly more artifacts. "
                               "The checkpoint downloads automatically on first use.",
                }),
                "device": (list(DEVICES), {
                    "tooltip": "auto: CUDA if available, then MPS, else CPU.",
                }),
                "output_format": (list(FORMATS), {
                    "tooltip": "FLAC is lossless (recommended for datasets); "
                               "WAV is universal; MP3 is 320 kbit/s.",
                }),
                "output_folder": ("STRING", {
                    "default": "", "multiline": False,
                    "placeholder": "empty = auto: output/voice/<media>",
                    "tooltip": "Destination folder. Empty = "
                               "<ComfyUI>/output/voice/<media name>. "
                               "Type any absolute or relative path.",
                }),
                "overwrite_mode": (["skip existing", "overwrite"], {
                    "tooltip": "What to do when stem files with the same name already "
                               "exist (skip adds _2, _3, ...).",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("voice_path", "status")
    OUTPUT_NODE = True
    FUNCTION = "process"
    CATEGORY = "audio"

    # ---------------------------------------------------------------- run ----

    def process(self, media_path, model, device, output_format,
                output_folder, overwrite_mode):
        ff.require_binaries()

        media_path = resolve_input_path(media_path)
        if not media_path or not os.path.isfile(media_path):
            raise ValueError(
                f"[VoiceBackgroundSeparator] media not found: {media_path or '(empty)'}"
            )
        if not ff.probe_has_audio(media_path):
            raise ValueError(
                f"[VoiceBackgroundSeparator] no audio stream in {media_path}"
            )

        stem_name = os.path.splitext(os.path.basename(media_path))[0]
        out_dir = resolve_output_dir(output_folder, "voice", stem_name)

        t0 = time.time()
        voice, sr, duration, dev = separate_to_tensors(media_path, model, device)

        ext = output_format
        exists = lambda c: os.path.exists(
            os.path.join(out_dir, f"{c}_voice.{ext}"))
        name = unique_name(out_dir, stem_name, exists, overwrite_mode)
        voice_path = os.path.join(out_dir, f"{name}_voice.{ext}")

        encode_stem(voice, sr, voice_path, ext)

        elapsed = time.time() - t0
        status = (f"model={model} device={dev} duration={duration:.1f}s "
                  f"elapsed={elapsed:.1f}s -> {out_dir}")
        print(f"[VoiceBackgroundSeparator] done: {status}")

        return (voice_path, status)


NODE_CLASS_MAPPINGS = {"VoiceBackgroundSeparator": VoiceBackgroundSeparator}
NODE_DISPLAY_NAME_MAPPINGS = {
    "VoiceBackgroundSeparator": "Voice Separator (Demucs)"
}
