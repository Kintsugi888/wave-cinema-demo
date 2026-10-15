"""Cast: per-project character reference images.

A project can hold up to MAX_CHARACTERS named characters, each with one
reference image (JPG/PNG/WEBP, <= MAX_IMAGE_BYTES). Images live at
data/projects/<id>/cast/<character_id>.<ext>; the workspace records
{character_id, name, filename, ext, bytes, sha256, created_at} under
state["characters"].

Character images are used three ways:
  1. the director restates character names + identity notes in every shot
     prompt (see direct.py);
  2. cloud/BYOK generation passes them as reference images to the
     provider (see providers.py — Higgsfield takes up to 9);
  3. the BYO job pack bundles them under cast/ so DIY generators get the
     same consistency references.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import ProjectNotFound, WaveCinemaError
from .workspace import ProjectWorkspace, project_dir

MAX_CHARACTERS = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


class CharacterRejected(WaveCinemaError):
    """A character upload was invalid (bad format, too big, cast full)."""


class CharacterNotFound(WaveCinemaError):
    """No such character exists in the project."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cast_dir(project_id: str) -> Path:
    d = project_dir(project_id) / "cast"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext_for(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def add_character(project_id: str, name: str, image_bytes: bytes,
                  filename: str) -> dict:
    """Add a named character with a reference image to the project.

    Raises CharacterRejected on bad format / oversize / full cast.
    Returns the character record.
    """
    name = (name or "").strip()[:80]
    if not name:
        raise CharacterRejected("character needs a name")
    ext = _ext_for(filename)
    if ext not in ALLOWED_EXTS:
        raise CharacterRejected(
            f"reference image must be JPG/PNG/WEBP (got {ext or 'no extension'})"
        )
    if not image_bytes:
        raise CharacterRejected("reference image is empty")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise CharacterRejected(
            f"reference image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
        )
    # magic-byte sniff: JPEG starts FFD8FF, PNG starts 89504E47, WEBP has RIFF....WEBP
    head = image_bytes[:12]
    looks_jpeg = head[:3] == b"\xff\xd8\xff"
    looks_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    looks_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (looks_jpeg or looks_png or looks_webp):
        raise CharacterRejected(
            "file does not look like a real image (magic bytes mismatch)"
        )

    ws = ProjectWorkspace.load(project_id)
    characters = ws.state.setdefault("characters", {})
    if len(characters) >= MAX_CHARACTERS:
        raise CharacterRejected(
            f"cast is full ({MAX_CHARACTERS} characters max)"
        )
    # names must be unique within a project (case-insensitive)
    if any(c["name"].lower() == name.lower() for c in characters.values()):
        raise CharacterRejected(f"a character named {name!r} is already in the cast")

    character_id = "ch_" + uuid.uuid4().hex[:8]
    dest = _cast_dir(project_id) / f"{character_id}{ext}"
    dest.write_bytes(image_bytes)

    record = {
        "character_id": character_id,
        "name": name,
        "filename": f"{character_id}{ext}",
        "ext": ext,
        "bytes": len(image_bytes),
        "sha256": _sha256(image_bytes),
        "created_at": _utcnow_iso(),
    }
    rev = ws.revision
    characters[character_id] = record
    ws.log_event(f"character added: {name!r} ({character_id}, {record['bytes']} bytes)")
    ws.save(expected_revision=rev)
    return dict(record)


def list_characters(project_id: str) -> list[dict]:
    """All characters in the project, oldest first."""
    ws = ProjectWorkspace.load(project_id)
    chars = ws.state.get("characters") or {}
    return [dict(chars[cid]) for cid in sorted(
        chars, key=lambda cid: chars[cid].get("created_at", ""))]


def get_character(project_id: str, character_id: str) -> dict:
    ws = ProjectWorkspace.load(project_id)
    characters = ws.state.get("characters") or {}
    record = characters.get(character_id)
    if record is None:
        raise CharacterNotFound(
            f"project {project_id} has no character {character_id!r}")
    return dict(record)


def character_image_path(project_id: str, character_id: str) -> Path:
    """Absolute path of a character's reference image (validated)."""
    record = get_character(project_id, character_id)
    path = (_cast_dir(project_id) / record["filename"]).resolve()
    # containment: the resolved path must stay inside the cast dir
    if _cast_dir(project_id).resolve() not in path.parents:
        raise CharacterNotFound("character image path escapes the cast dir")
    if not path.is_file():
        raise CharacterNotFound(
            f"reference image file missing for {character_id!r}")
    return path


def character_reference_paths(project_id: str) -> list[Path]:
    """Reference image paths for generation, oldest first (capped at 9)."""
    return [character_image_path(project_id, c["character_id"])
            for c in list_characters(project_id)[:9]]


def remove_character(project_id: str, character_id: str) -> dict:
    """Delete a character and its reference image (revision-guarded)."""
    ws = ProjectWorkspace.load(project_id)
    characters = ws.state.get("characters") or {}
    record = characters.get(character_id)
    if record is None:
        raise CharacterNotFound(
            f"project {project_id} has no character {character_id!r}")
    try:
        path = character_image_path(project_id, character_id)
    except CharacterNotFound:
        path = None
    rev = ws.revision
    del characters[character_id]
    ws.log_event(f"character removed: {record['name']!r} ({character_id})")
    ws.save(expected_revision=rev)
    if path is not None:
        path.unlink(missing_ok=True)
    return {"character_id": character_id, "deleted": True}
