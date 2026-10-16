"""Wave Cinema durable storage adapter.

Provides Postgres-backed implementations of the registry functions.
Falls back to JSON files if DATABASE_URL is not set (local development).
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

USE_DB = bool(os.environ.get("DATABASE_URL"))

if USE_DB:
    from db import get_session, Project as DBProject


def _json_path(name: str) -> Path:
    return Path(__file__).parent / "data" / name


def _load_json_fallback(name: str, default: Any) -> Any:
    try:
        return json.loads(_json_path(name).read_text())
    except Exception:
        return default


def _save_json_fallback(name: str, data: Any) -> None:
    p = _json_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


# --- Project registry ---

def registry_list(user_id: Optional[str] = None) -> list:
    if not USE_DB:
        items = _load_json_fallback("projects.json", [])
        if not isinstance(items, list):
            return []
        if user_id:
            items = [i for i in items
                     if i.get("owner_id") == user_id or "owner_id" not in i]
        return items
    s = get_session()
    try:
        q = s.query(DBProject)
        if user_id:
            q = q.filter(
                (DBProject.owner_id == user_id) |
                (DBProject.owner_id.is_(None))
            )
        return [{"id": p.id, "name": p.name, "owner_id": p.owner_id}
                for p in q.all()]
    finally:
        s.close()


def registry_add(pid: str, name: str, user_id: Optional[str] = None) -> None:
    if not USE_DB:
        items = [i for i in _load_json_fallback("projects.json", [])
                 if isinstance(i, dict) and i.get("id") != pid]
        entry = {"id": pid, "name": name}
        if user_id:
            entry["owner_id"] = user_id
        items.append(entry)
        _save_json_fallback("projects.json", items)
        return
    s = get_session()
    try:
        p = s.query(DBProject).filter_by(id=pid).first()
        if p:
            p.name = name
            if user_id:
                p.owner_id = user_id
        else:
            p = DBProject(id=pid, name=name, owner_id=user_id)
            s.add(p)
        s.commit()
    finally:
        s.close()


def registry_remove(pid: str) -> None:
    if not USE_DB:
        items = [i for i in _load_json_fallback("projects.json", [])
                 if isinstance(i, dict) and i.get("id") != pid]
        _save_json_fallback("projects.json", items)
        return
    s = get_session()
    try:
        s.query(DBProject).filter_by(id=pid).delete()
        s.commit()
    finally:
        s.close()


# --- Users ---

def get_user(user_id: str):
    if not USE_DB:
        users = _load_json_fallback("users.json", [])
        for u in users:
            if isinstance(u, dict) and u.get("id") == user_id:
                return u
        return None
    from db import User as DBUser
    s = get_session()
    try:
        u = s.query(DBUser).filter_by(id=user_id).first()
        if not u:
            return None
        return {"id": u.id, "email": u.email, "name": u.name,
                "credits": u.credits, "byok_keys": u.byok_keys}
    finally:
        s.close()


def get_user_by_email(email: str):
    email = (email or "").strip().lower()
    if not USE_DB:
        users = _load_json_fallback("users.json", [])
        for u in users:
            if isinstance(u, dict) and (u.get("email") or "").lower() == email:
                return u
        return None
    from db import User as DBUser
    s = get_session()
    try:
        u = s.query(DBUser).filter_by(email=email).first()
        if not u:
            return None
        return {"id": u.id, "email": u.email, "name": u.name,
                "credits": u.credits, "byok_keys": u.byok_keys}
    finally:
        s.close()


def save_user(user: dict) -> None:
    if not USE_DB:
        users = _load_json_fallback("users.json", [])
        users = [u for u in users if isinstance(u, dict)
                 and u.get("id") != user.get("id")]
        users.append(user)
        _save_json_fallback("users.json", users)
        return
    from db import User as DBUser
    s = get_session()
    try:
        u = s.query(DBUser).filter_by(id=user["id"]).first()
        if u:
            u.email = user.get("email", u.email)
            u.name = user.get("name", u.name)
            u.credits = user.get("credits", u.credits or 0)
            if "byok_keys" in user:
                u.byok_keys = user["byok_keys"]
        else:
            u = DBUser(
                id=user["id"],
                email=user.get("email", ""),
                name=user.get("name", ""),
                credits=user.get("credits", 0),
                byok_keys=user.get("byok_keys", "{}"),
            )
            s.add(u)
        s.commit()
    finally:
        s.close()


# --- API tokens ---

def token_list(user_id: str) -> list:
    if not USE_DB:
        tokens = _load_json_fallback("api_tokens.json", [])
        out = [{k: v for k, v in t.items() if k != "token_hash"}
               for t in tokens
               if isinstance(t, dict) and t.get("user_id") == user_id]
        out.sort(key=lambda t: t.get("created_at", 0), reverse=True)
        return out
    from db import ApiToken as DBToken
    s = get_session()
    try:
        tokens = (s.query(DBToken)
                  .filter_by(user_id=user_id, revoked=0)
                  .order_by(DBToken.created_at.desc())
                  .all())
        return [{"id": t.id, "user_id": t.user_id, "name": t.name,
                 "created_at": int(t.created_at.timestamp()) if t.created_at else 0,
                 "last_used_at": int(t.last_used_at.timestamp()) if t.last_used_at else None}
                for t in tokens]
    finally:
        s.close()


def token_create(record: dict) -> None:
    if not USE_DB:
        tokens = _load_json_fallback("api_tokens.json", [])
        tokens.append(record)
        _save_json_fallback("api_tokens.json", tokens)
        return
    from db import ApiToken as DBToken
    s = get_session()
    try:
        t = DBToken(
            id=record["id"],
            user_id=record["user_id"],
            name=record.get("name", ""),
            token_hash=record["token_hash"],
        )
        s.add(t)
        s.commit()
    finally:
        s.close()


def token_revoke(user_id: str, token_id: str) -> bool:
    if not USE_DB:
        tokens = _load_json_fallback("api_tokens.json", [])
        kept = [t for t in tokens
                if not (isinstance(t, dict) and t.get("id") == token_id
                        and t.get("user_id") == user_id)]
        if len(kept) == len(tokens):
            return False
        _save_json_fallback("api_tokens.json", kept)
        return True
    from db import ApiToken as DBToken
    s = get_session()
    try:
        t = (s.query(DBToken)
             .filter_by(id=token_id, user_id=user_id, revoked=0)
             .first())
        if not t:
            return False
        t.revoked = 1
        s.commit()
        return True
    finally:
        s.close()


def token_verify_by_hash(token_hash: str):
    if not USE_DB:
        tokens = _load_json_fallback("api_tokens.json", [])
        for t in tokens:
            if isinstance(t, dict) and t.get("token_hash") == token_hash:
                return t
        return None
    from db import ApiToken as DBToken
    s = get_session()
    try:
        t = (s.query(DBToken)
             .filter_by(token_hash=token_hash, revoked=0)
             .first())
        if not t:
            return None
        return {"id": t.id, "user_id": t.user_id, "name": t.name,
                "token_hash": t.token_hash}
    finally:
        s.close()


# --- Pending quotes (transient, but durable in DB) ---

def quotes_load() -> dict:
    if not USE_DB:
        q = _load_json_fallback("quotes.json", {})
        return q if isinstance(q, dict) else {}
    from db import Quote as DBQuote
    s = get_session()
    try:
        return {q.project_id: q.data for q in s.query(DBQuote).all()
                if isinstance(q.data, dict)}
    finally:
        s.close()


def quotes_save(project_id: str, data: dict) -> None:
    if not USE_DB:
        q = quotes_load()
        q[project_id] = data
        _save_json_fallback("quotes.json", q)
        return
    from db import Quote as DBQuote
    import uuid
    s = get_session()
    try:
        rec = s.query(DBQuote).filter_by(project_id=project_id).first()
        if rec:
            rec.data = data
        else:
            rec = DBQuote(id=f"q_{uuid.uuid4().hex[:12]}",
                          project_id=project_id, data=data)
            s.add(rec)
        s.commit()
    finally:
        s.close()
