"""Export: verify the assembled master, record bytes + SHA-256."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .workspace import ProjectWorkspace, project_dir


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def export_master(project_id: str) -> dict:
    """Verify exports/master.mp4 exists; record bytes + SHA-256 in workspace.

    Returns the download record {export_id, download_path, bytes, sha256}.
    """
    master = project_dir(project_id) / "exports" / "master.mp4"
    if not master.exists():
        raise FileNotFoundError(
            f"project {project_id} has no assembled master — assemble first"
        )
    size = master.stat().st_size
    if size == 0:
        raise ValueError("assembled master is empty")
    digest = _sha256(master)

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    export_id = "exp_" + uuid.uuid4().hex[:12]
    record = {
        "export_id": export_id,
        "download_path": str(master),
        "bytes": size,
        "sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ws.state["exports"][export_id] = record
    ws.log_event(
        f"exported master.mp4 ({size} bytes, sha256 {digest[:12]}…)"
    )
    ws.save(expected_revision=rev)
    return dict(record)
