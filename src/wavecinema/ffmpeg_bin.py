"""Bundled ffmpeg binary resolution.

Render/HF free tiers don't ship ffmpeg on PATH. imageio-ffmpeg bundles a
static binary; this module exposes it and (when available) prepends its
directory to PATH so bare ``"ffmpeg"`` subprocess calls work from any
entry point (uvicorn main:app, tests, scripts).

Import this module early (e.g. at the top of app/main.py) on hosts
without a system ffmpeg.
"""
from __future__ import annotations

import os
from pathlib import Path

_EXE: str | None = None


def exe() -> str:
    """Absolute path to a working ffmpeg binary, or ``"ffmpeg"`` fallback."""
    global _EXE
    if _EXE is None:
        try:
            import imageio_ffmpeg  # type: ignore

            _EXE = str(Path(imageio_ffmpeg.get_ffmpeg_exe()))
        except Exception:
            _EXE = "ffmpeg"
    return _EXE


def ensure_on_path() -> str:
    """Prepend the bundled binary's directory to PATH; return the exe path."""
    path = exe()
    if path != "ffmpeg":
        bindir = str(Path(path).parent)
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    return path


# Apply on import: bare "ffmpeg" resolves everywhere afterwards.
ensure_on_path()
