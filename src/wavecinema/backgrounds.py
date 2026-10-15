"""Locations & Backgrounds: per-project environment reference images.

Mirrors characters.py. A project can hold up to MAX_BACKGROUNDS named
backgrounds, each with one reference image (JPG/PNG/WEBP, <= MAX_IMAGE_BYTES).
Images live at data/projects/<id>/backgrounds/<background_id>.<ext>; the
workspace records {background_id, name, filename, ext, bytes, sha256,
created_at} under state["backgrounds"].

Background images are used four ways:
  1. the director restates location names in every shot prompt
     (see direct.py — LOCATION REFERENCES block);
  2. cloud/BYOK generation passes them as reference images to the
     provider alongside character refs (capped at 9 total refs per scene);
  3. the BYO job pack bundles them under backgrounds/ so DIY generators
     get the same environment consistency references;
  4. the Nano Banana background generator auto-saves here.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import ProjectNotFound, WaveCinemaError
from .workspace import ProjectWorkspace, project_dir

MAX_BACKGROUNDS = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


class BackgroundRejected(WaveCinemaError):
    """A background upload was invalid (bad format, too big, list full)."""


class BackgroundNotFound(WaveCinemaError):
    """No such background exists in the project."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backgrounds_dir(project_id: str) -> Path:
    d = project_dir(project_id) / "backgrounds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext_for(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def add_background(project_id: str, name: str, image_bytes: bytes,
                   filename: str) -> dict:
    """Add a named background with a reference image to the project.

    Raises BackgroundRejected on bad format / oversize / full list.
    Returns the background record.
    """
    name = (name or "").strip()[:80]
    if not name:
        raise BackgroundRejected("background needs a name")
    ext = _ext_for(filename)
    if ext not in ALLOWED_EXTS:
        raise BackgroundRejected(
            f"reference image must be JPG/PNG/WEBP (got {ext or 'no extension'})"
        )
    if not image_bytes:
        raise BackgroundRejected("reference image is empty")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise BackgroundRejected(
            f"reference image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
        )
    # magic-byte sniff: JPEG starts FFD8FF, PNG starts 89504E47, WEBP has RIFF....WEBP
    head = image_bytes[:12]
    looks_jpeg = head[:3] == b"\xff\xd8\xff"
    looks_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    looks_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (looks_jpeg or looks_png or looks_webp):
        raise BackgroundRejected(
            "file does not look like a real image (magic bytes mismatch)"
        )

    ws = ProjectWorkspace.load(project_id)
    backgrounds = ws.state.setdefault("backgrounds", {})
    if len(backgrounds) >= MAX_BACKGROUNDS:
        raise BackgroundRejected(
            f"background list is full ({MAX_BACKGROUNDS} max)"
        )
    # names must be unique within a project (case-insensitive)
    if any(b["name"].lower() == name.lower() for b in backgrounds.values()):
        raise BackgroundRejected(
            f"a background named {name!r} already exists")

    background_id = "bg_" + uuid.uuid4().hex[:8]
    dest = _backgrounds_dir(project_id) / f"{background_id}{ext}"
    dest.write_bytes(image_bytes)

    record = {
        "background_id": background_id,
        "name": name,
        "filename": f"{background_id}{ext}",
        "ext": ext,
        "bytes": len(image_bytes),
        "sha256": _sha256(image_bytes),
        "created_at": _utcnow_iso(),
    }
    rev = ws.revision
    backgrounds[background_id] = record
    ws.log_event(f"background added: {name!r} ({background_id}, {record['bytes']} bytes)")
    ws.save(expected_revision=rev)
    return dict(record)


def list_backgrounds(project_id: str) -> list[dict]:
    """All backgrounds in the project, oldest first."""
    ws = ProjectWorkspace.load(project_id)
    bgs = ws.state.get("backgrounds") or {}
    return [dict(bgs[bid]) for bid in sorted(
        bgs, key=lambda bid: bgs[bid].get("created_at", ""))]


def get_background(project_id: str, background_id: str) -> dict:
    ws = ProjectWorkspace.load(project_id)
    backgrounds = ws.state.get("backgrounds") or {}
    record = backgrounds.get(background_id)
    if record is None:
        raise BackgroundNotFound(
            f"project {project_id} has no background {background_id!r}")
    return dict(record)


def background_image_path(project_id: str, background_id: str) -> Path:
    """Absolute path of a background's reference image (validated)."""
    record = get_background(project_id, background_id)
    path = (_backgrounds_dir(project_id) / record["filename"]).resolve()
    # containment: the resolved path must stay inside the backgrounds dir
    if _backgrounds_dir(project_id).resolve() not in path.parents:
        raise BackgroundNotFound("background image path escapes the dir")
    if not path.is_file():
        raise BackgroundNotFound(
            f"reference image file missing for {background_id!r}")
    return path


def background_reference_paths(project_id: str) -> list[Path]:
    """Reference image paths for generation, oldest first (capped at 9)."""
    return [background_image_path(project_id, b["background_id"])
            for b in list_backgrounds(project_id)[:9]]


def remove_background(project_id: str, background_id: str) -> dict:
    """Delete a background and its reference image (revision-guarded)."""
    ws = ProjectWorkspace.load(project_id)
    backgrounds = ws.state.get("backgrounds") or {}
    record = backgrounds.get(background_id)
    if record is None:
        raise BackgroundNotFound(
            f"project {project_id} has no background {background_id!r}")
    try:
        path = background_image_path(project_id, background_id)
    except BackgroundNotFound:
        path = None
    rev = ws.revision
    del backgrounds[background_id]
    ws.log_event(f"background removed: {record['name']!r} ({background_id})")
    ws.save(expected_revision=rev)
    if path is not None:
        path.unlink(missing_ok=True)
    return {"background_id": background_id, "deleted": True}
