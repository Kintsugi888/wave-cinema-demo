"""Versioned JSON project state with revision-guarded writes.

Each project owns one workspace.json. Every mutating write must carry the
expected revision the caller read; a stale revision is rejected with
RevisionConflict and nothing is overwritten. This makes long, resumable
productions safe against concurrent writers clobbering each other.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import ProjectNotFound, RevisionConflict

DATA_DIR = Path.home() / "workspace" / "wave-cinema" / "data"
PROJECTS_DIR = DATA_DIR / "projects"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def workspace_path(project_id: str) -> Path:
    return project_dir(project_id) / "workspace.json"


def _new_workspace(project_id: str, name: str) -> dict:
    now = _utcnow_iso()
    return {
        "schema_version": "wavecinema.workspace/1",
        "project_id": project_id,
        "name": name,
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "rights": {"statement_version": None, "assented": False, "assented_at": None},
        "song": None,            # {filename, ext, bytes, sha256, duration_ms}
        "cue_sheet": None,       # {words, phrases, generated_by, ...}
        "characters": {},        # character_id -> {name, filename, ext, bytes, sha256, created_at}
        "scenes": [],            # cut plan: [{scene_id, start_ms, end_ms, duration_ms, ...}]
        "bible": None,           # production bible JSON
        "plan": None,            # timed plan JSON {schema_version, scenes:[{scene_id,...prompt}]}
        "scene_audio": {},       # scene_id -> {path, bytes, sha256}
        "quotes": {},            # quote_id -> quote dict
        "jobs": {},              # idempotency_key -> generation job record
        "takes": {},             # scene_id -> [take dicts {take_id, path, bytes, sha256, ...}]
        "selections": {},        # scene_id -> take_id (human's winning take)
        "exports": {},           # export_id -> {path, bytes, sha256, created_at}
        "log": [],               # append-only human-readable event log (bounded)
    }


def create_project(name: str) -> str:
    """Create a new project directory + workspace.json at revision 0."""
    project_id = "wc_" + uuid.uuid4().hex[:12]
    pdir = project_dir(project_id)
    pdir.mkdir(parents=True, exist_ok=False)
    (pdir / "takes").mkdir(exist_ok=True)
    (pdir / "scenes").mkdir(exist_ok=True)
    (pdir / "exports").mkdir(exist_ok=True)
    ws = _new_workspace(project_id, name)
    with open(workspace_path(project_id), "w", encoding="utf-8") as f:
        json.dump(ws, f, indent=2)
    return project_id


class ProjectWorkspace:
    """Load-modify-save handle for one project's workspace.json.

    Typical use:
        ws = ProjectWorkspace.load(project_id)
        rev = ws.revision
        ws.state["scenes"] = [...]
        ws.save(expected_revision=rev)   # raises RevisionConflict if stale
    """

    def __init__(self, project_id: str, state: dict):
        self.project_id = project_id
        self.state = state

    @property
    def revision(self) -> int:
        return int(self.state["revision"])

    @classmethod
    def load(cls, project_id: str) -> "ProjectWorkspace":
        path = workspace_path(project_id)
        if not path.exists():
            raise ProjectNotFound(f"no such project: {project_id}")
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return cls(project_id, state)

    def snapshot(self) -> dict:
        """Deep copy of the current state (for callers that only read)."""
        return copy.deepcopy(self.state)

    def save(self, expected_revision: int) -> int:
        """Persist state, guarding on the caller's expected revision.

        Returns the new revision. Raises RevisionConflict without writing
        anything when expected_revision != current revision.
        """
        path = workspace_path(self.project_id)
        if not path.exists():
            raise ProjectNotFound(f"no such project: {self.project_id}")
        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        current = int(on_disk["revision"])
        if int(expected_revision) != current:
            raise RevisionConflict(
                f"stale write rejected: expected_revision={expected_revision}, "
                f"current revision={current} (project {self.project_id})"
            )
        new_rev = current + 1
        self.state["revision"] = new_rev
        self.state["updated_at"] = _utcnow_iso()
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        tmp.replace(path)
        return new_rev

    def log_event(self, message: str) -> None:
        """Append a bounded human-readable event to the workspace log."""
        log = self.state.setdefault("log", [])
        log.append({"at": _utcnow_iso(), "message": message})
        # keep the log bounded so workspace.json stays small
        if len(log) > 500:
            del log[: len(log) - 500]
