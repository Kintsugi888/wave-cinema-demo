"""Assembly: trim selected takes to exact scene ranges, concatenate,
lay the unshifted master audio at PTS 0. Take audio is discarded.
Assembly costs zero credits.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .ingest import master_path
from .workspace import ProjectWorkspace, project_dir


def _scene_duration(scene: dict) -> float:
    return (scene["end_ms"] - scene["start_ms"]) / 1000.0


def assemble_project(project_id: str) -> dict:
    """Assemble the master MP4 at data/projects/<id>/exports/master.mp4."""
    ws = ProjectWorkspace.load(project_id)
    plan = ws.state.get("plan")
    scenes = (plan or {}).get("scenes") or ws.state.get("scenes") or []
    if not scenes:
        raise ValueError(f"project {project_id} has no scene plan — direct first")

    master = master_path(project_id)
    out_dir = project_dir(project_id) / "exports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "master.mp4"

    # Every scene needs a selected take.
    missing = [s["scene_id"] for s in scenes
               if s["scene_id"] not in ws.state["selections"]]
    if missing:
        raise ValueError(
            f"cannot assemble: scenes without a selected take: {missing}"
        )
    takes_by_id = {}
    for take_list in ws.state["takes"].values():
        for t in take_list:
            takes_by_id[t["take_id"]] = t

    with tempfile.TemporaryDirectory(prefix="wc-assemble-") as tmp:
        tmpdir = Path(tmp)
        trimmed: list[Path] = []
        for scene in scenes:
            take_id = ws.state["selections"][scene["scene_id"]]
            take = takes_by_id.get(take_id)
            if take is None or not Path(take["path"]).exists():
                raise ValueError(
                    f"selected take {take_id} for {scene['scene_id']} is missing"
                )
            seg = tmpdir / f"{scene['scene_id']}.mp4"
            # Trim the take to the exact scene duration; drop its audio.
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-i", take["path"],
                 "-t", f"{_scene_duration(scene):.3f}",
                 "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-an", str(seg)],
                check=True,
            )
            trimmed.append(seg)

        concat_list = tmpdir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{p}'\n" for p in trimmed), encoding="utf-8"
        )
        video_only = tmpdir / "video.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(video_only)],
            check=True,
        )
        # Lay the unshifted master at PTS 0 under the picture.
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-i", str(video_only), "-i", str(master),
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-shortest", str(out_path)],
            check=True,
        )

    result = {
        "path": str(out_path),
        "bytes": out_path.stat().st_size,
    }
    rev = ws.revision
    ws.log_event(f"assembled master.mp4 ({result['bytes']} bytes, 0 credits)")
    ws.save(expected_revision=rev)
    return result
