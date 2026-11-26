"""Shared helpers for output folders and collision-safe file naming."""

from __future__ import annotations

import os

try:
    import folder_paths

    def output_root() -> str:
        return folder_paths.get_output_directory()
except Exception:  # standalone use / tests (no ComfyUI)
    def output_root() -> str:
        return os.path.join(os.getcwd(), "output")


def clean_path(p: str) -> str:
    """Trim whitespace/quotes and expand ~ in user-supplied paths."""
    p = (p or "").strip().strip('"').strip("'")
    return os.path.expanduser(p) if p else ""


def _relative_candidates(rel: str):
    """Where a relative name could live inside a ComfyUI installation.

    Order: input dir, output dir, then the ComfyUI base folder (covers
    names like 'input/clip.webm' or 'output/stems/voice.flac'). Without
    ComfyUI, fall back to the CWD.
    """
    try:
        import folder_paths

        cands = [
            os.path.join(folder_paths.get_input_directory(), rel),
            os.path.join(folder_paths.get_output_directory(), rel),
        ]
        base = getattr(folder_paths, "base_path", None)
        if base:
            cands.append(os.path.join(base, rel))
    except Exception:  # standalone use / tests (no ComfyUI)
        cands = [os.path.join(os.getcwd(), rel)]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_input_path(value: str) -> str:
    """Resolve a user-supplied media/subtitle path flexibly.

    1. empty -> "" ; absolute paths (after quote stripping / ~ expansion)
       are returned untouched - pasting any absolute path keeps working.
    2. A relative name is resolved to the first EXISTING match among the
       ComfyUI candidates (_relative_candidates), e.g. a file placed into
       <ComfyUI>/input/ (ComfyUI's own upload widgets do this).
    3. No match -> the cleaned name as-is, so downstream code raises its
       usual clear 'not found' error showing exactly what was typed.
    """
    p = clean_path(value)
    if not p or os.path.isabs(p):
        return p
    rel = p.replace("\\", "/")
    for cand in _relative_candidates(rel):
        if os.path.isfile(cand):
            return cand
    return p


def resolve_output_dir(user_path: str, default_subdir: str, media_stem: str) -> str:
    """Use the user's folder or fall back to <output>/<default_subdir>/<media_stem>."""
    p = clean_path(user_path)
    if not p:
        p = os.path.join(output_root(), default_subdir, media_stem)
    os.makedirs(p, exist_ok=True)
    return p


def unique_name(out_dir, base, exists_fn, overwrite_mode="overwrite", used=None):
    """Collision-safe file stem name.

    - `used` tracks names created in the current run (always renamed with _2, _3...).
    - `exists_fn(candidate)` reports whether files from earlier runs exist;
      they are reused in 'overwrite' mode and avoided in 'skip existing' mode.
    """
    used = used if used is not None else set()
    candidate, i = base, 2
    while True:
        exists = exists_fn(candidate)
        if (candidate not in used) and (not exists or overwrite_mode == "overwrite"):
            used.add(candidate)
            return candidate
        candidate = f"{base}_{i}"
        i += 1
