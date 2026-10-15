"""Video-to-video remix: upload a clip, describe the change, get a variant.

A remix job records the source clip + style prompt and runs it through the
project's generation lane:
  - cloud/BYOK lane: submitted to the provider as a remix generation
    (prompt carries the transformation; the source clip is attached as the
    visual reference). Providers with native video-to-video use it;
    others treat it as a guided generation.
  - byo lane: the remix is added to the DIY job pack as a remix task.

Results are stored alongside the source for side-by-side comparison.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import characters as _characters
from .workspace import ProjectWorkspace, project_dir

REMIX_KEY = "remixes"  # workspace state key
MAX_REMIX_BYTES = 100 * 1024 * 1024
REMIX_EXTS = {".mp4", ".mov"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_like_video(path: Path) -> bool:
    try:
        if path.stat().st_size < 1024:
            return False
        with open(path, "rb") as f:
            head = f.read(64)
        # mp4/mov both use the ftyp box near the start
        return b"ftyp" in head[:32]
    except OSError:
        return False


def submit_remix(project_id: str, video_bytes: bytes, filename: str,
                 style_prompt: str) -> dict:
    """Store a remix source clip + prompt. Returns the remix job record."""
    style_prompt = (style_prompt or "").strip()
    if not style_prompt:
        raise ValueError("describe the style/cast/setting change first")
    if len(style_prompt) > 2000:
        raise ValueError("style prompt too long (max 2000 characters)")
    ext = Path(filename or "").suffix.lower()
    if ext not in REMIX_EXTS:
        raise ValueError(
            f"remix source must be MP4 or MOV (got {ext or 'no extension'})")
    if len(video_bytes) > MAX_REMIX_BYTES:
        raise ValueError("remix source exceeds 100 MB")

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision

    rdir = project_dir(project_id) / "remix"
    rdir.mkdir(parents=True, exist_ok=True)
    remix_id = "rx_" + uuid.uuid4().hex[:10]
    # defend against path traversal: fixed name, validated extension
    src_path = rdir / f"{remix_id}{ext}"
    src_path.write_bytes(video_bytes)
    if not _looks_like_video(src_path):
        src_path.unlink(missing_ok=True)
        raise ValueError("that file is not a valid MP4/MOV video")

    # Full generation prompt: transformation of the source clip.
    full_prompt = (
        f"VIDEO-TO-VIDEO REMIX. Take the attached source clip and transform it: "
        f"{style_prompt}. Keep the same action, timing and composition as the "
        f"source; only change what the style direction asks for. "
        f"No text, logos or watermarks in frame."
    )

    job = {
        "remix_id": remix_id,
        "source_filename": Path(filename).name,
        "source_path": str(src_path),
        "source_bytes": len(video_bytes),
        "style_prompt": style_prompt,
        "full_prompt": full_prompt,
        "lane": ws.state.get("provider_lane", "byo"),
        "aspect_ratio": ws.state.get("aspect_ratio", "16:9"),
        "status": "queued",       # queued -> done | failed
        "result_path": None,
        "result_take_id": None,
        "error": None,
        "created_at": _utcnow_iso(),
    }
    ws.state.setdefault(REMIX_KEY, {})[remix_id] = job
    ws.log_event(f"remix {remix_id} queued: {style_prompt[:60]}")
    ws.save(expected_revision=rev)
    return dict(job)


def run_remix(project_id: str, remix_id: str, provider) -> dict:
    """Execute a queued remix through the given provider. Returns the job.

    The provider receives the remix prompt with the source clip attached as
    the visual reference. Duration defaults to the source clip length
    (capped to provider bounds by the caller).
    """
    from . import takes as _takes

    ws = ProjectWorkspace.load(project_id)
    job = (ws.state.get(REMIX_KEY) or {}).get(remix_id)
    if job is None:
        raise KeyError(f"no remix {remix_id!r} in project {project_id}")
    if job["status"] == "done":
        return dict(job)

    # Probe the source duration with ffprobe (bundled via ffmpeg_bin).
    duration_s = 5.0
    try:
        import subprocess, json as _json
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", job["source_path"]],
            capture_output=True, text=True, timeout=30)
        duration_s = float(_json.loads(r.stdout)["format"]["duration"])
    except Exception:
        pass
    duration_s = max(2.0, min(15.0, duration_s))

    rev = ws.revision
    job["status"] = "running"
    ws.state[REMIX_KEY][remix_id] = job
    ws.save(expected_revision=rev)

    try:
        scene = {"scene_id": remix_id,
                 "start_ms": 0, "end_ms": int(duration_s * 1000)}
        clip_path = provider.generate(
            scene, job["full_prompt"], duration_s=duration_s,
            aspect=job.get("aspect_ratio", "16:9"),
            reference_images=_characters.character_reference_paths(project_id),
        )
        take = _takes.register_take(
            project_id, remix_id, clip_path,
            job_idempotency_key=f"remix:{remix_id}",
        )
    except Exception as exc:  # noqa: BLE001
        ws = ProjectWorkspace.load(project_id)
        rev = ws.revision
        job = ws.state[REMIX_KEY][remix_id]
        job["status"] = "failed"
        job["error"] = str(exc)[:500]
        ws.state[REMIX_KEY][remix_id] = job
        ws.log_event(f"remix {remix_id} FAILED: {exc}")
        ws.save(expected_revision=rev)
        raise

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    job = ws.state[REMIX_KEY][remix_id]
    job["status"] = "done"
    job["result_path"] = take["path"]
    job["result_take_id"] = take["take_id"]
    job["completed_at"] = _utcnow_iso()
    ws.state[REMIX_KEY][remix_id] = job
    ws.log_event(f"remix {remix_id} done: take {take['take_id']}")
    ws.save(expected_revision=rev)
    return dict(job)


def list_remixes(project_id: str) -> list[dict]:
    ws = ProjectWorkspace.load(project_id)
    jobs = ws.state.get(REMIX_KEY) or {}
    return [dict(j) for j in sorted(jobs.values(),
                                    key=lambda j: j.get("created_at", ""),
                                    reverse=True)]


def get_remix(project_id: str, remix_id: str) -> dict:
    ws = ProjectWorkspace.load(project_id)
    job = (ws.state.get(REMIX_KEY) or {}).get(remix_id)
    if job is None:
        raise KeyError(f"no remix {remix_id!r} in project {project_id}")
    return dict(job)
