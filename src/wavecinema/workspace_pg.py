"""Postgres-backed project workspace storage.

Drop-in replacement for workspace.py's file-based storage when
DATABASE_URL is set. Stores the full workspace dict as JSONB in the
`projects` table (shared with the UI layer's app/db.py).

The revision-guard semantics are preserved: save() raises
RevisionConflict on stale writes.
"""

from __future__ import annotations

import copy
import os
import uuid

from .errors import ProjectNotFound, RevisionConflict
from .workspace import _new_workspace, _utcnow_iso

_USE_PG = bool(os.environ.get("DATABASE_URL"))


def _get_db():
    """Import the UI's db module.

    Tries direct import first (works when app/ is on sys.path, which it
    is when running via main.py). Falls back to path search for
    standalone backend usage.
    """
    import sys
    try:
        import db as dbmod
        return dbmod
    except ImportError:
        pass
    from pathlib import Path
    # Candidate locations for the app directory
    here = Path(__file__).resolve()
    candidates = [
        # Render: /opt/render/project/src/app
        here.parent.parent.parent / "app",
        # Local dev: ~/workspace/wave-cinema/deploy/hf-space/app
        here.parent.parent.parent / "deploy" / "hf-space" / "app",
    ]
    for app_dir in candidates:
        if (app_dir / "db.py").exists():
            if str(app_dir) not in sys.path:
                sys.path.insert(0, str(app_dir))
            import db as dbmod
            return dbmod
    raise ImportError("Could not locate app/db.py for Postgres backend")


def create_project_pg(name: str) -> str:
    """Create a project in Postgres. Returns project_id."""
    dbmod = _get_db()
    project_id = "wc_" + uuid.uuid4().hex[:12]
    ws = _new_workspace(project_id, name)
    s = dbmod.get_session()
    try:
        p = dbmod.Project(id=project_id, name=name, workspace=ws)
        s.add(p)
        s.commit()
    finally:
        s.close()
    return project_id


class ProjectWorkspacePG:
    """Postgres-backed workspace handle with revision guards."""

    def __init__(self, project_id: str, state: dict):
        self.project_id = project_id
        self.state = state

    @property
    def revision(self) -> int:
        return int(self.state["revision"])

    @classmethod
    def load(cls, project_id: str) -> "ProjectWorkspacePG":
        dbmod = _get_db()
        s = dbmod.get_session()
        try:
            p = s.query(dbmod.Project).filter_by(id=project_id).first()
            if not p or not p.workspace:
                raise ProjectNotFound(f"no such project: {project_id}")
            # Detach the dict from the session
            state = copy.deepcopy(p.workspace)
            return cls(project_id, state)
        finally:
            s.close()

    def snapshot(self) -> dict:
        return copy.deepcopy(self.state)

    def save(self, expected_revision: int) -> int:
        dbmod = _get_db()
        s = dbmod.get_session()
        try:
            p = s.query(dbmod.Project).filter_by(id=self.project_id).first()
            if not p or not p.workspace:
                raise ProjectNotFound(f"no such project: {self.project_id}")
            current = int(p.workspace.get("revision", 0))
            if int(expected_revision) != current:
                raise RevisionConflict(
                    f"stale write rejected: expected_revision={expected_revision}, "
                    f"current revision={current} (project {self.project_id})"
                )
            new_rev = current + 1
            self.state["revision"] = new_rev
            self.state["updated_at"] = _utcnow_iso()
            # SQLAlchemy needs a new dict object to detect JSON change
            p.workspace = copy.deepcopy(self.state)
            s.commit()
            return new_rev
        finally:
            s.close()
