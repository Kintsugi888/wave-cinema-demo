"""Take store: generated clips persisted per scene with bytes + SHA-256.

Takes live at data/projects/<id>/takes/<take_id>.mp4. Registration,
selection and deletion are all recorded in the workspace under revision
guard. A scene's selected take cannot be deleted until another is selected.
"""
from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

from .errors import SceneNotFound, TakeNotFound, TakeProtected
from .workspace import ProjectWorkspace, project_dir


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _scene_ids(ws: ProjectWorkspace) -> list[str]:
    plan = ws.state.get("plan") or {}
    scenes = plan.get("scenes") or ws.state.get("scenes") or []
    return [s["scene_id"] for s in scenes]


def register_take(project_id: str, scene_id: str, clip_path: str | Path,
                  quote_id: str | None = None,
                  job_idempotency_key: str | None = None) -> dict:
    """Copy a generated clip into the take store and record it."""
    src = Path(clip_path)
    if not src.exists():
        raise FileNotFoundError(f"clip not found: {clip_path}")
    ws = ProjectWorkspace.load(project_id)
    if scene_id not in _scene_ids(ws):
        raise SceneNotFound(f"project {project_id} has no scene {scene_id!r}")

    take_id = f"{scene_id}-t{uuid.uuid4().hex[:8]}"
    dest = project_dir(project_id) / "takes" / f"{take_id}.mp4"
    shutil.copy2(src, dest)

    take = {
        "take_id": take_id,
        "scene_id": scene_id,
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
        "quote_id": quote_id,
        "job_idempotency_key": job_idempotency_key,
    }
    rev = ws.revision
    ws.state["takes"].setdefault(scene_id, []).append(take)
    ws.log_event(
        f"take {take_id} registered for {scene_id} "
        f"({take['bytes']} bytes, sha256 {take['sha256'][:12]}…)"
    )
    ws.save(expected_revision=rev)
    return dict(take)


def list_takes(project_id: str, scene_id: str) -> list[dict]:
    ws = ProjectWorkspace.load(project_id)
    if scene_id not in _scene_ids(ws):
        raise SceneNotFound(f"project {project_id} has no scene {scene_id!r}")
    return [dict(t) for t in ws.state["takes"].get(scene_id, [])]


def get_selection(project_id: str, scene_id: str) -> str | None:
    ws = ProjectWorkspace.load(project_id)
    return ws.state["selections"].get(scene_id)


def select_take(project_id: str, scene_id: str, take_id: str) -> dict:
    """Record the human's winning take for a scene (revision-guarded)."""
    ws = ProjectWorkspace.load(project_id)
    takes = ws.state["takes"].get(scene_id, [])
    if not any(t["take_id"] == take_id for t in takes):
        raise TakeNotFound(f"take {take_id!r} not found for scene {scene_id!r}")
    rev = ws.revision
    ws.state["selections"][scene_id] = take_id
    ws.log_event(f"selected take {take_id} for scene {scene_id}")
    ws.save(expected_revision=rev)
    return {"scene_id": scene_id, "take_id": take_id,
            "revision": ws.revision}


def delete_take(project_id: str, scene_id: str, take_id: str) -> dict:
    """Delete a take. Refuses while it is the scene's selected take."""
    ws = ProjectWorkspace.load(project_id)
    if ws.state["selections"].get(scene_id) == take_id:
        raise TakeProtected(
            f"take {take_id!r} is selected for scene {scene_id!r}; "
            "select another take first"
        )
    takes = ws.state["takes"].get(scene_id, [])
    take = next((t for t in takes if t["take_id"] == take_id), None)
    if take is None:
        raise TakeNotFound(f"take {take_id!r} not found for scene {scene_id!r}")
    rev = ws.revision
    ws.state["takes"][scene_id] = [t for t in takes if t["take_id"] != take_id]
    Path(take["path"]).unlink(missing_ok=True)
    ws.log_event(f"deleted take {take_id} ({scene_id})")
    ws.save(expected_revision=rev)
    return {"scene_id": scene_id, "take_id": take_id, "deleted": True}
