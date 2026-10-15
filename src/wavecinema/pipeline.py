"""Pipeline orchestration and structural human gates.

Gates (enforced, not advisory):
  - first_scene_gate: scene 0 must have a human-selected take before any
    other scene may generate. generate_rest() refuses otherwise.
"""
from __future__ import annotations

from .errors import GateBlocked, SceneNotFound
from .workspace import ProjectWorkspace


def _ordered_scene_ids(ws: ProjectWorkspace) -> list[str]:
    plan = ws.state.get("plan") or {}
    scenes = plan.get("scenes") or ws.state.get("scenes") or []
    return [s["scene_id"] for s in scenes]


def first_scene_gate(project_id: str) -> bool:
    """True only if scene 0 has a human-selected take (reviewed)."""
    ws = ProjectWorkspace.load(project_id)
    scene_ids = _ordered_scene_ids(ws)
    if not scene_ids:
        return False
    first = scene_ids[0]
    selected = ws.state["selections"].get(first)
    if not selected:
        return False
    takes = {t["take_id"] for t in ws.state["takes"].get(first, [])}
    return selected in takes


def assert_first_scene_gate(project_id: str) -> None:
    if not first_scene_gate(project_id):
        raise GateBlocked(
            "first-scene gate: generate and get a human-selected take for "
            "scene 0 before generating any other scene"
        )


def generate_rest(project_id: str, idempotency_prefix: str) -> list[dict]:
    """Generate takes for every scene after scene 0.

    Each scene needs its own approved, unexpired quote (quote + approve per
    scene first). Refuses entirely when the first-scene gate is not
    satisfied. Returns one result dict per generated scene.
    """
    from . import api  # local import: api imports this module for gates

    assert_first_scene_gate(project_id)
    ws = ProjectWorkspace.load(project_id)
    scene_ids = _ordered_scene_ids(ws)
    if not scene_ids:
        raise SceneNotFound(f"project {project_id} has no scenes")
    results: list[dict] = []
    for scene_id in scene_ids[1:]:
        results.append(api.generate_scene(
            project_id, scene_id,
            idempotency_key=f"{idempotency_prefix}:{scene_id}",
        ))
    return results
