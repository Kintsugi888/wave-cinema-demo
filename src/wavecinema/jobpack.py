"""BYO generation: job packs for testers with their own working accounts.

A job pack is a zip a tester downloads from the web UI, generates clips for
on their own Muse / video-gen account, and uploads back. Zero new
credentials needed on either side: the pack carries per-scene prompts +
timing, and the clips come back as scene_00.mp4 … scene_NN.mp4 in a zip.

Pack layout:
    pack.json                 # project + per-scene timing/cast/prompts
    prompts/scene_00.txt …    # full prompt text per scene
    stills/…                  # any scene stills found in the project dir (optional)
    cast/<character_id>.jpg   # character reference images (optional)
    README.txt                # instructions for the generating friend

Clip zip layout (uploaded back):
    scene_00.mp4 … scene_NN.mp4   (also accepts sNN.mp4)
each registered as a take with bytes + SHA-256, zero credits (BYO).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import characters as _characters
from .errors import SceneNotFound
from .takes import register_take
from .workspace import ProjectWorkspace, project_dir

CLIP_RE = re.compile(r"^(?:scene_(\d+)|s(\d+))\.mp4$", re.IGNORECASE)
MAX_IMPORT_BYTES = 500 * 1024 * 1024

README_TEXT = """WAVE CINEMA — BYO JOB PACK
===========================

You are generating the motion clips for this film on YOUR OWN Muse /
video-generation account. The app never sees your account.

HOW TO GENERATE
1. Open prompts/scene_00.txt … scene_NN.txt — one self-contained prompt
   per scene. Animate each scene's still (in stills/, if present) or use
   the prompt as a text-to-video prompt.
2. Keep every clip 16:9, 24 fps if you can. Length per scene is in
   pack.json (duration_ms). A little over is fine; the edit trims.
3. Character identity is non-negotiable: match the identity locks stated
   in each prompt exactly (hair, eyes, skin, wardrobe). If cast/ holds
   reference images, match each named character to their photo exactly —
   same face in every scene. No text, logos or watermarks in frame.
   No morphing between poses.

HOW TO SEND BACK
4. Name your clips scene_00.mp4, scene_01.mp4, … matching the prompt
   numbers. Zip them (any zip name) and upload the zip in the app at
   Review → "Import BYO clips".
5. The app registers each clip as a take (bytes + SHA-256), you review
   and select winners, then Assemble builds the master. Zero credits
   change hands — this is your own generation.

pack.json is the machine-readable source of truth (scene ids, exact
millisecond boundaries, cast, prompts).
"""


def _scenes(ws: ProjectWorkspace) -> list[dict]:
    plan = ws.state.get("plan") or {}
    scenes = plan.get("scenes") or ws.state.get("scenes") or []
    if not scenes:
        raise SceneNotFound("no timed plan — direct the project first")
    return scenes


def _find_stills(pdir: Path) -> list[Path]:
    stills: list[Path] = []
    for cand in (pdir / "stills", pdir / "scenes"):
        if cand.is_dir():
            stills.extend(
                p for p in sorted(cand.iterdir())
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            )
    return stills


def build_pack(project_id: str) -> Path:
    """Build the BYO job-pack zip for a project. Returns the zip path."""
    ws = ProjectWorkspace.load(project_id)
    scenes = _scenes(ws)
    pdir = project_dir(project_id)

    pack_scenes = []
    prompt_files: dict[str, str] = {}
    for i, s in enumerate(scenes):
        sid = str(s.get("scene_id", f"s{i:02d}"))
        prompt = str(s.get("prompt", ""))
        cast = s.get("cast", "")
        if isinstance(cast, list):
            cast = ", ".join(str(c) for c in cast)
        pack_scenes.append({
            "scene_id": sid,
            "file_index": i,
            "clip_name": f"scene_{i:02d}.mp4",
            "start_ms": int(s.get("start_ms", 0)),
            "end_ms": int(s.get("end_ms", 0)),
            "duration_ms": int(s.get("duration_ms",
                                     s.get("end_ms", 0) - s.get("start_ms", 0))),
            "cast": cast,
            "prompt": prompt,
        })
        prompt_files[f"prompts/scene_{i:02d}.txt"] = (
            f"SCENE {sid}  ({pack_scenes[-1]['start_ms']}–{pack_scenes[-1]['end_ms']} ms)\n"
            f"CAST: {cast}\n\n{prompt}\n"
        )

    pack = {
        "format": "wavecinema.jobpack/1",
        "project_id": project_id,
        "project_name": ws.state.get("name", project_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rights_statement": (ws.state.get("rights") or {}).get("statement_version"),
        "scene_count": len(pack_scenes),
        "scenes": pack_scenes,
        "characters": [
            {"character_id": c["character_id"], "name": c["name"],
             "file": f"cast/{c['filename']}"}
            for c in _characters.list_characters(project_id)
        ],
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = pdir / "exports" / f"wc-jobpack-{ts}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.json", json.dumps(pack, indent=2))
        z.writestr("README.txt", README_TEXT)
        for name, text in prompt_files.items():
            z.writestr(name, text)
        for still in _find_stills(pdir):
            z.write(still, f"stills/{still.name}")
        for c in _characters.list_characters(project_id):
            try:
                img = _characters.character_image_path(
                    project_id, c["character_id"])
            except Exception:
                continue
            z.write(img, f"cast/{c['filename']}")

    rev = ws.revision
    ws.log_event(f"BYO job pack built: {out.name} ({len(pack_scenes)} scenes)")
    ws.save(expected_revision=rev)
    return out


def _looks_like_mp4(path: Path) -> bool:
    try:
        if path.stat().st_size < 1024:
            return False
        with open(path, "rb") as f:
            head = f.read(64)
        return b"ftyp" in head[:32]
    except OSError:
        return False


def _clip_scene_id(filename: str, scene_ids: set[str]) -> str | None:
    m = CLIP_RE.match(filename)
    if not m:
        return None
    idx = int(m.group(1) or m.group(2))
    sid = f"s{idx:02d}"
    return sid if sid in scene_ids else None


def import_clips_zip(project_id: str, zip_bytes: bytes) -> dict:
    """Register user-generated clips from an uploaded zip as takes.

    Zero credits — the clips were generated on the tester's own account.
    Returns {"imported": [...], "skipped": [...]}.
    """
    if len(zip_bytes) > MAX_IMPORT_BYTES:
        raise ValueError("clip zip exceeds 500 MB")
    if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
        raise ValueError("upload is not a zip file")

    ws = ProjectWorkspace.load(project_id)
    scenes = _scenes(ws)
    scene_ids = {str(s.get("scene_id")) for s in scenes}

    imported: list[dict] = []
    skipped: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="wc-byo-") as tmp:
        tmpdir = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > MAX_IMPORT_BYTES:
                    continue
                # defend against zip-slip: only the basename is used
                name = Path(info.filename).name
                if not name:
                    continue
                dest = tmpdir / name
                with z.open(info) as src, open(dest, "wb") as fh:
                    shutil.copyfileobj(src, fh)

        for clip in sorted(tmpdir.iterdir()):
            if not clip.is_file():
                continue
            sid = _clip_scene_id(clip.name, scene_ids)
            if sid is None:
                skipped.append({"file": clip.name,
                                "reason": "name does not match scene_XX.mp4 for a known scene"})
                continue
            if not _looks_like_mp4(clip):
                skipped.append({"file": clip.name,
                                "reason": "not a valid mp4 (ftyp box missing)"})
                continue
            take = register_take(
                project_id, sid, clip,
                job_idempotency_key=f"byo:{clip.name}",
            )
            imported.append({
                "file": clip.name,
                "scene_id": sid,
                "take_id": take["take_id"],
                "bytes": take["bytes"],
                "sha256": take["sha256"],
            })

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.log_event(f"BYO import: {len(imported)} takes registered, "
                 f"{len(skipped)} files skipped")
    ws.save(expected_revision=rev)
    return {"imported": imported, "skipped": skipped}
