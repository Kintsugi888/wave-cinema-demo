"""Credit ledger: prepaid, closed-loop credits with idempotent settlement.

SQLite at ~/workspace/wave-cinema/data/ledger.db, one append-only `entries`
table. Funds flow: grant/purchase -> hold (reserved) -> capture (debit) or
release (refund). Holds reduce the available balance while outstanding.

Idempotency: every entry carries a UNIQUE idempotency_key. Repeating a
hold/debit/capture/release with the same key returns the existing entry —
an interrupted run can never double-spend.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import InsufficientFunds
from .workspace import DATA_DIR

LEDGER_PATH = DATA_DIR / "ledger.db"

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('grant','hold','debit','refund','purchase')),
    credits INTEGER NOT NULL CHECK (credits > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_account ON entries (account_id);
"""


def _connect() -> sqlite3.Connection:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LEDGER_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Client-side production ledger for one Wave Cinema installation."""

    def grant(self, account_id: str, credits: int, memo: str = "",
              idempotency_key: str | None = None) -> dict:
        """Add credits to an account (promotional grant)."""
        if credits <= 0:
            raise ValueError("grant credits must be positive")
        key = idempotency_key or f"grant:{uuid.uuid4().hex}"
        with _lock, _connect() as conn:
            existing = conn.execute(
                "SELECT * FROM entries WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                return _row_to_dict(existing)
            cur = conn.execute(
                "INSERT INTO entries (account_id, kind, credits, idempotency_key, memo, created_at)"
                " VALUES (?, 'grant', ?, ?, ?, ?)",
                (account_id, credits, key, memo, _utcnow_iso()),
            )
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)

    def hold(self, account_id: str, credits: int, idempotency_key: str,
             memo: str = "") -> dict:
        """Reserve credits against an approved quote (idempotent on key)."""
        if credits <= 0:
            raise ValueError("hold credits must be positive")
        with _lock, _connect() as conn:
            existing = conn.execute(
                "SELECT * FROM entries WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return _row_to_dict(existing)  # no double hold
            if self._balance_locked(conn, account_id) < credits:
                raise InsufficientFunds(
                    f"account {account_id!r}: cannot hold {credits} credits "
                    f"(balance {self._balance_locked(conn, account_id)})"
                )
            cur = conn.execute(
                "INSERT INTO entries (account_id, kind, credits, idempotency_key, memo, created_at)"
                " VALUES (?, 'hold', ?, ?, ?, ?)",
                (account_id, credits, idempotency_key, memo, _utcnow_iso()),
            )
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)

    def capture(self, hold_id: int) -> dict:
        """Settle a hold into a debit. Idempotent; returns the debit entry."""
        with _lock, _connect() as conn:
            hold = self._get_locked(conn, hold_id)
            if hold["kind"] != "hold":
                raise ValueError(f"entry {hold_id} is a {hold['kind']}, not a hold")
            if conn.execute(
                "SELECT 1 FROM entries WHERE idempotency_key = ?",
                (f"release:{hold_id}",),
            ).fetchone():
                raise ValueError(f"hold {hold_id} was already released")
            debit_key = f"capture:debit:{hold_id}"
            existing = conn.execute(
                "SELECT * FROM entries WHERE idempotency_key = ?", (debit_key,)
            ).fetchone()
            if existing:
                return _row_to_dict(existing)
            now = _utcnow_iso()
            # Release the reservation, then book the spend: net == one debit.
            conn.execute(
                "INSERT INTO entries (account_id, kind, credits, idempotency_key, memo, created_at)"
                " VALUES (?, 'refund', ?, ?, ?, ?)",
                (hold["account_id"], hold["credits"],
                 f"capture:release:{hold_id}",
                 f"auto-release of hold {hold_id} on capture", now),
            )
            cur = conn.execute(
                "INSERT INTO entries (account_id, kind, credits, idempotency_key, memo, created_at)"
                " VALUES (?, 'debit', ?, ?, ?, ?)",
                (hold["account_id"], hold["credits"], debit_key,
                 f"capture of hold {hold_id} ({hold['memo']})", now),
            )
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)

    def release(self, hold_id: int) -> dict:
        """Release a hold back to the account as a refund. Idempotent."""
        with _lock, _connect() as conn:
            hold = self._get_locked(conn, hold_id)
            if hold["kind"] != "hold":
                raise ValueError(f"entry {hold_id} is a {hold['kind']}, not a hold")
            if conn.execute(
                "SELECT 1 FROM entries WHERE idempotency_key = ?",
                (f"capture:debit:{hold_id}",),
            ).fetchone():
                raise ValueError(f"hold {hold_id} was already captured")
            refund_key = f"release:{hold_id}"
            existing = conn.execute(
                "SELECT * FROM entries WHERE idempotency_key = ?", (refund_key,)
            ).fetchone()
            if existing:
                return _row_to_dict(existing)
            cur = conn.execute(
                "INSERT INTO entries (account_id, kind, credits, idempotency_key, memo, created_at)"
                " VALUES (?, 'refund', ?, ?, ?, ?)",
                (hold["account_id"], hold["credits"], refund_key,
                 f"release of hold {hold_id} ({hold['memo']})", _utcnow_iso()),
            )
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)

    def balance(self, account_id: str) -> int:
        """Available credits: grants/purchases/refunds minus holds/debits."""
        with _lock, _connect() as conn:
            return self._balance_locked(conn, account_id)

    def get_entry(self, entry_id: int) -> dict:
        with _lock, _connect() as conn:
            return self._get_locked(conn, entry_id)

    def history(self, account_id: str, limit: int = 200) -> list[dict]:
        with _lock, _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entries WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    # -- internals ------------------------------------------------------
    @staticmethod
    def _get_locked(conn: sqlite3.Connection, entry_id: int) -> dict:
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no ledger entry {entry_id}")
        return _row_to_dict(row)

    @staticmethod
    def _balance_locked(conn: sqlite3.Connection, account_id: str) -> int:
        row = conn.execute(
            """SELECT COALESCE(SUM(CASE
                 WHEN kind IN ('grant','purchase','refund') THEN credits
                 WHEN kind IN ('hold','debit') THEN -credits
                 ELSE 0 END), 0) AS bal
               FROM entries WHERE account_id = ?""",
            (account_id,),
        ).fetchone()
        return int(row["bal"])
