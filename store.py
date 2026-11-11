"""SQLite storage. Each record is a JSON document in a table named for its kind
(leads, quotes, messages, events, invoices), with a few indexed columns for
lookups. One file under DATA_DIR; safe for one server process."""
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from typing import Any, Dict, List, Optional

KINDS = ("leads", "quotes", "messages", "events", "invoices")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(os.environ.get("DATA_DIR", tempfile.gettempdir()), "vectorsales.db")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        for kind in KINDS:
            self._db.execute(
                f"CREATE TABLE IF NOT EXISTS {kind} (id TEXT PRIMARY KEY, lead_id TEXT, "
                "created_at TEXT, data TEXT NOT NULL)"
            )
            self._db.execute(f"CREATE INDEX IF NOT EXISTS {kind}_lead ON {kind}(lead_id)")
        self._db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.commit()

    # --- records ---------------------------------------------------------------

    def put(self, table: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        assert table in KINDS
        doc.setdefault("id", uuid.uuid4().hex[:12])
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO {table} (id, lead_id, created_at, data) VALUES (?, ?, ?, ?)",
                (doc["id"], doc.get("lead_id") or (doc["id"] if table == "leads" else None),
                 doc.get("created_at", ""), json.dumps(doc)),
            )
            self._db.commit()
        return doc

    def get(self, table: str, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(f"SELECT data FROM {table} WHERE id = ?", (doc_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, table: str, lead_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, args = f"SELECT data FROM {table}", ()
        if lead_id:
            sql, args = sql + " WHERE lead_id = ?", (lead_id,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def find(self, table: str, **match: Any) -> List[Dict[str, Any]]:
        return [d for d in self.list(table) if all(d.get(k) == v for k, v in match.items())]

    def delete_where(self, table: str, **match: Any) -> None:
        for doc in self.find(table, **match):
            with self._lock:
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (doc["id"],))
        with self._lock:
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            for kind in KINDS:
                self._db.execute(f"DELETE FROM {kind}")
            self._db.execute("DELETE FROM settings WHERE key = 'clock_offset'")
            self._db.commit()

    # --- settings --------------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            self._db.commit()
