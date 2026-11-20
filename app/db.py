"""SQLite storage for the queue. One table, JSON columns for flexible data."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Statuses:
#   pending     -> waiting to be processed (vision + search)
#   processing  -> worker is busy with it
#   review      -> candidates ready, waiting for the user's pick
#   added       -> added to the Discogs collection
#   not_found   -> marked by the user: not in Discogs (submit manually later)
#   skipped     -> skipped (not added)
#   error       -> processing failed
STATUSES = ("pending", "processing", "review", "added", "not_found", "skipped", "error")

JSON_COLS = ("photos", "extracted", "candidates", "error_info", "rare", "rare_alerted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    source TEXT NOT NULL DEFAULT 'upload',
    hint TEXT NOT NULL DEFAULT '',
    provider TEXT,
    photos TEXT NOT NULL DEFAULT '[]',
    extracted TEXT,
    candidates TEXT,
    release_id INTEGER,
    instance_id INTEGER,
    folder_id INTEGER,
    media_condition TEXT,
    sleeve_condition TEXT,
    notes TEXT,
    error TEXT,
    error_info TEXT,
    rare TEXT,
    rare_alerted TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        # Lightweight migrations for databases created by older versions.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(items)")}
        for col in ("error_info", "rare", "rare_alerted"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
                if col == "rare_alerted":
                    # Databases from the first rare-find version already alerted for the current flag.
                    self._conn.execute(
                        "UPDATE items SET rare_alerted = json_array(json_extract(rare, '$.release_id'))"
                        " WHERE rare IS NOT NULL AND json_extract(rare, '$.release_id') IS NOT NULL")
        self._lock = threading.Lock()
        # After a restart: put half-processed items back in the queue.
        self._conn.execute("UPDATE items SET status='pending' WHERE status='processing'")

    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for c in JSON_COLS:
            d[c] = json.loads(d[c]) if d.get(c) else ([] if c in ("photos", "candidates") else None)
        return d

    def create(self, *, photos: list[dict], hint: str = "", source: str = "upload", provider: str | None = None) -> dict:
        with self._lock:
            ts = _now()
            cur = self._conn.execute(
                "INSERT INTO items (created_at, updated_at, status, source, hint, provider, photos) VALUES (?,?,?,?,?,?,?)",
                (ts, ts, "pending", source, hint, provider, json.dumps(photos)),
            )
            return self.get(cur.lastrowid)  # type: ignore[arg-type]

    def get(self, item_id: int) -> dict | None:
        return self._row(self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def list(self, statuses: list[str] | None = None, limit: int = 500, rare_only: bool = False) -> list[dict]:
        where, args = [], []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += statuses
        if rare_only:
            where.append("rare IS NOT NULL")
        q = "SELECT * FROM items" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(q, (*args, limit)).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
        out = {s: 0 for s in STATUSES}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    def update(self, item_id: int, **fields: Any) -> dict | None:
        if not fields:
            return self.get(item_id)
        with self._lock:
            sets, vals = [], []
            for k, v in fields.items():
                if k in JSON_COLS and v is not None:
                    v = json.dumps(v)
                sets.append(f"{k}=?")
                vals.append(v)
            sets.append("updated_at=?")
            vals.append(_now())
            self._conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*vals, item_id))
        return self.get(item_id)

    def claim_next(self) -> dict | None:
        """Atomically take the oldest pending item and mark it processing."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM items WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE items SET status='processing', error=NULL, error_info=NULL, updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        return self.get(row["id"])

    def requeue_errors(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE items SET status='pending', error=NULL, error_info=NULL, updated_at=? WHERE status='error'",
                (_now(),),
            )
            return cur.rowcount

    def reserve_rare_alert(self, item_id: int, release_id: int) -> bool:
        """Record that this release was alerted for the item. True only for the first caller."""
        with self._lock:
            row = self._conn.execute("SELECT rare_alerted FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return False
            alerted = json.loads(row["rare_alerted"]) if row["rare_alerted"] else []
            if release_id in alerted:
                return False
            self._conn.execute("UPDATE items SET rare_alerted=? WHERE id=?",
                               (json.dumps([*alerted, release_id]), item_id))
            return True

    def delete(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM items WHERE id=?", (item_id,))
