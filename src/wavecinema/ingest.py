"""Song ingest: accept common audio bytes, probe exact duration, store by SHA-256."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from . import ffmpeg_bin
from .errors import FormatRejected
from .workspace import ProjectWorkspace, project_dir

FFMPEG = ffmpeg_bin.exe()

# ffmpeg decodes all of these; the phone file picker and backend stay in sync.
ACCEPTED = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".m4a": "m4a",
    ".aac": "aac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".flac": "flac",
}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")


def _ffmpeg_duration_ms(path: Path) -> int:
    """Exact duration in integer milliseconds via `ffmpeg -i` stderr parsing.

    ffprobe is not shipped on free hosting tiers; ffmpeg (bundled via
    imageio-ffmpeg) always is, and its input probe prints the duration.
    """
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    m = _DURATION_RE.search(out.stderr)
    if not m:
        raise FormatRejected(
            f"could not probe duration of {path.name}; "
            "file may be corrupt or not audio"
        )
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return int(round(((h * 60 + mi) * 60 + s) * 1000))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_song_bytes(project_id: str, file_bytes: bytes, filename: str) -> dict:
    """Store the master song for a project.

    Writes data/projects/<id>/master.<ext>, records {filename, ext, bytes,
    sha256, duration_ms} in the workspace. Only the formats in ACCEPTED.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ACCEPTED:
        raise FormatRejected(
            f"unsupported audio format {ext!r} for {filename!r}; "
            f"accepted: {sorted(ACCEPTED)}"
        )
    if not file_bytes:
        raise FormatRejected(f"empty upload for {filename!r}")

    pdir = project_dir(project_id)
    master_path = pdir / f"master{ext}"
    master_path.write_bytes(file_bytes)

    # Validate that ffmpeg can actually decode it (rejects corrupt files).
    duration_ms = _ffmpeg_duration_ms(master_path)
    if duration_ms <= 0:
        master_path.unlink(missing_ok=True)
        raise FormatRejected(f"{filename!r} decoded to zero duration")

    digest = _sha256(master_path)
    size = master_path.stat().st_size

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["song"] = {
        "filename": filename,
        "ext": ext,
        "path": str(master_path),
        "bytes": size,
        "sha256": digest,
        "duration_ms": duration_ms,
    }
    ws.log_event(f"ingested {filename} ({size} bytes, sha256 {digest[:12]}…, {duration_ms} ms)")
    ws.save(expected_revision=rev)
    return dict(ws.state["song"])


def master_path(project_id: str) -> Path:
    """Locate the stored master audio file for a project."""
    ws = ProjectWorkspace.load(project_id)
    song = ws.state.get("song")
    if not song:
        raise FormatRejected(f"project {project_id} has no ingested song")
    return Path(song["path"])
