"""Scene audio slicing: one exact asset per scene, window at local zero.

Deterministic: same scene window + same master = same asset bytes (given
the same ffmpeg build). Slices are 44.1 kHz stereo WAV.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .ingest import master_path
from .workspace import ProjectWorkspace, project_dir

SLICE_SR = 44100


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slice_scenes(project_id: str) -> list[dict]:
    """Cut the master into per-scene WAVs under data/projects/<id>/scenes/."""
    master = master_path(project_id)
    ws = ProjectWorkspace.load(project_id)
    scenes = ws.state.get("scenes") or []
    if not scenes:
        raise ValueError(f"project {project_id} has no cut plan — plan cuts first")

    out_dir = project_dir(project_id) / "scenes"
    out_dir.mkdir(exist_ok=True)

    results: list[dict] = []
    for scene in scenes:
        scene_id = scene["scene_id"]
        start_s = scene["start_ms"] / 1000.0
        dur_s = scene["duration_ms"] / 1000.0
        out_path = out_dir / f"{scene_id}.wav"
        # Input seeking (-ss before -i) then -t: output starts at local zero.
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}",
             "-i", str(master),
             "-c:a", "pcm_s16le", "-ar", str(SLICE_SR), "-ac", "2",
             str(out_path)],
            check=True,
        )
        info = {
            "scene_id": scene_id,
            "path": str(out_path),
            "bytes": out_path.stat().st_size,
            "sha256": _sha256(out_path),
            "start_ms": scene["start_ms"],
            "duration_ms": scene["duration_ms"],
        }
        results.append(info)

    rev = ws.revision
    ws.state["scene_audio"] = {r["scene_id"]: r for r in results}
    ws.log_event(f"sliced {len(results)} scene audio assets")
    ws.save(expected_revision=rev)
    return results
