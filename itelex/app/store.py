"""SQLite-Speicher: Nachrichten, Anrufliste, Sperrliste, Einstellungen."""

from __future__ import annotations

import ipaddress
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,          -- in / out
    peer TEXT NOT NULL,               -- Nummer oder 'unbekannt'
    peer_name TEXT DEFAULT '',
    answerback TEXT DEFAULT '',
    text TEXT NOT NULL,
    status TEXT DEFAULT '',           -- queued / sending / delivered / sent / failed
    detail TEXT DEFAULT '',
    read INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer, ts);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    peer TEXT DEFAULT '',
    peer_name TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    answerback TEXT DEFAULT '',
    result TEXT DEFAULT '',
    duration REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blocklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT UNIQUE NOT NULL,     -- Telexnummer oder IP-Adresse
    note TEXT DEFAULT '',
    resolved_ip TEXT DEFAULT '',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS kept (peer TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grafiken (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    note TEXT DEFAULT '',
    ts REAL NOT NULL
);
"""

MIGRATIONS = [
    ("messages", "greet", "ALTER TABLE messages ADD COLUMN greet INTEGER DEFAULT 1"),
    ("messages", "start_line", "ALTER TABLE messages ADD COLUMN start_line TEXT"),
    ("messages", "end_line", "ALTER TABLE messages ADD COLUMN end_line TEXT"),
    ("messages", "ab", "ALTER TABLE messages ADD COLUMN ab INTEGER DEFAULT 0"),
]

_NUM_RE = re.compile(r"\d{3,}")


def number_from_answerback(answerback: str) -> str | None:
    m = _NUM_RE.search(answerback or "")
    return m.group(0) if m else None


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            for table, col, sql in MIGRATIONS:
                cols = [r[1] for r in self._db.execute(f"PRAGMA table_info({table})")]
                if col not in cols:
                    self._db.execute(sql)
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            self._db.commit()
            return rows

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid or cur.rowcount

    # --- Nachrichten ---------------------------------------------------------
    def add_message(self, direction: str, peer: str, text: str, *, peer_name: str = "",
                    answerback: str = "", status: str = "", detail: str = "",
                    read: bool = False, greet: bool = True, start_line: str | None = None,
                    end_line: str | None = None, ab: bool = False) -> int:
        return self._x(
            "INSERT INTO messages(ts,direction,peer,peer_name,answerback,text,status,detail,read,greet,"
            "start_line,end_line,ab) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), direction, peer, peer_name, answerback, text, status, detail,
             1 if read else 0, 1 if greet else 0, start_line, end_line, 1 if ab else 0))

    def update_message(self, mid: int, **fields) -> None:
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE messages SET {cols} WHERE id=?", (*fields.values(), mid))

    def delete_message(self, mid: int) -> None:
        self._x("DELETE FROM messages WHERE id=?", (mid,))

    def message(self, mid: int) -> dict | None:
        rows = self._q("SELECT * FROM messages WHERE id=?", (mid,))
        return rows[0] if rows else None

    def messages(self, peer: str, limit: int = 500) -> list[dict]:
        return self._q("SELECT * FROM (SELECT * FROM messages WHERE peer=? ORDER BY ts DESC LIMIT ?)"
                       " ORDER BY ts", (peer, limit))

    def conversations(self) -> list[dict]:
        return self._q("""
            SELECT m.peer,
                   COALESCE((SELECT name FROM contacts c WHERE c.number=m.peer),
                            (SELECT peer_name FROM messages p WHERE p.peer=m.peer AND p.peer_name!=''
                              ORDER BY ts DESC LIMIT 1)) AS name,
                   (SELECT 1 FROM contacts c WHERE c.number=m.peer) AS in_book,
                   (SELECT 1 FROM kept k WHERE k.peer=m.peer) AS kept,
                   (SELECT answerback FROM messages p WHERE p.peer=m.peer AND p.answerback!=''
                     ORDER BY ts DESC LIMIT 1) AS answerback,
                   MAX(m.ts) AS last_ts,
                   SUM(CASE WHEN m.read=0 AND m.direction='in' THEN 1 ELSE 0 END) AS unread,
                   (SELECT text FROM messages l WHERE l.peer=m.peer ORDER BY ts DESC LIMIT 1) AS last_text,
                   (SELECT direction FROM messages l WHERE l.peer=m.peer ORDER BY ts DESC LIMIT 1) AS last_dir
            FROM messages m GROUP BY m.peer ORDER BY last_ts DESC""")

    def mark_read(self, peer: str) -> None:
        self._x("UPDATE messages SET read=1 WHERE peer=?", (peer,))

    def unread_count(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM messages WHERE read=0 AND direction='in'")[0]["n"]

    def last_incoming(self) -> dict | None:
        rows = self._q("SELECT * FROM messages WHERE direction='in' ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def delete_conversation(self, peer: str) -> None:
        self._x("DELETE FROM messages WHERE peer=?", (peer,))
        self._x("DELETE FROM kept WHERE peer=?", (peer,))

    def set_kept(self, peer: str, keep: bool) -> None:
        if keep:
            self._x("INSERT OR REPLACE INTO kept(peer, ts) VALUES(?,?)", (peer, time.time()))
        else:
            self._x("DELETE FROM kept WHERE peer=?", (peer,))

    def is_kept(self, peer: str) -> bool:
        return bool(self._q("SELECT 1 FROM kept WHERE peer=?", (peer,)))

    def find_peer_by_name(self, name: str) -> str | None:
        key = name.lower().strip()
        rows = self._q("SELECT number FROM contacts WHERE lower(name)=? LIMIT 1", (key,))
        if not rows:
            rows = self._q("SELECT number FROM contacts WHERE lower(name) LIKE ? ORDER BY length(name) LIMIT 1",
                           (f"%{key}%",))
        if rows:
            return rows[0]["number"]
        rows = self._q("SELECT peer FROM messages WHERE (lower(peer_name) LIKE ? OR lower(answerback) LIKE ?)"
                       " AND peer GLOB '[0-9]*' ORDER BY ts DESC LIMIT 1",
                       (f"%{name.lower()}%", f"%{name.lower()}%"))
        return rows[0]["peer"] if rows else None

    # --- Eigene Grafiken ------------------------------------------------------
    def grafiken(self) -> list[dict]:
        return self._q("SELECT * FROM grafiken ORDER BY lower(name)")

    def save_grafik(self, name: str, text: str) -> int:
        name = name.strip()[:40]
        if not name:
            raise ValueError("Bitte einen Namen angeben")
        existing = self._q("SELECT id FROM grafiken WHERE lower(name)=lower(?)", (name,))
        if existing:
            self._x("UPDATE grafiken SET text=?, ts=? WHERE id=?", (text, time.time(), existing[0]["id"]))
            return existing[0]["id"]
        return self._x("INSERT INTO grafiken(name,text,ts) VALUES(?,?,?)", (name, text, time.time()))

    def delete_grafik(self, gid: int) -> None:
        self._x("DELETE FROM grafiken WHERE id=?", (gid,))

    # --- Telefonbuch -----------------------------------------------------------
    def contacts(self) -> list[dict]:
        return self._q("SELECT * FROM contacts ORDER BY lower(name)")

    def save_contact(self, number: str, name: str, note: str = "", cid: int | None = None) -> int:
        number, name = number.strip(), name.strip()
        if not number.isdigit():
            raise ValueError("Telexnummer darf nur Ziffern enthalten")
        if not name:
            raise ValueError("Bitte einen Namen angeben")
        if cid:
            self._x("UPDATE contacts SET number=?, name=?, note=? WHERE id=?", (number, name, note, cid))
            return cid
        existing = self._q("SELECT id FROM contacts WHERE number=?", (number,))
        if existing:
            self._x("UPDATE contacts SET name=?, note=? WHERE id=?", (name, note, existing[0]["id"]))
            return existing[0]["id"]
        return self._x("INSERT INTO contacts(number,name,note,ts) VALUES(?,?,?,?)",
                       (number, name, note, time.time()))

    def delete_contact(self, cid: int) -> None:
        self._x("DELETE FROM contacts WHERE id=?", (cid,))

    def contact_name(self, number: str | None) -> str:
        if not number:
            return ""
        rows = self._q("SELECT name FROM contacts WHERE number=?", (number,))
        return rows[0]["name"] if rows else ""

    # --- Anrufliste ------------------------------------------------------------
    def add_call(self, direction: str, *, peer: str = "", peer_name: str = "", ip: str = "",
                 answerback: str = "", result: str = "", duration: float = 0) -> int:
        return self._x(
            "INSERT INTO calls(ts,direction,peer,peer_name,ip,answerback,result,duration)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), direction, peer, peer_name, ip, answerback, result, duration))

    def calls(self, limit: int = 300) -> list[dict]:
        return self._q("SELECT * FROM calls ORDER BY ts DESC LIMIT ?", (limit,))

    # --- Sperrliste --------------------------------------------------------------
    def block(self, pattern: str, note: str = "", resolved_ip: str = "") -> int:
        return self._x("INSERT OR REPLACE INTO blocklist(pattern,note,resolved_ip,ts) VALUES(?,?,?,?)",
                       (pattern, note, resolved_ip, time.time()))

    def unblock(self, bid: int) -> None:
        self._x("DELETE FROM blocklist WHERE id=?", (bid,))

    def blocklist(self) -> list[dict]:
        return self._q("SELECT * FROM blocklist ORDER BY ts DESC")

    def set_resolved_ip(self, bid: int, ip: str) -> None:
        self._x("UPDATE blocklist SET resolved_ip=? WHERE id=?", (ip, bid))

    def is_ip_blocked(self, ip: str) -> bool:
        return bool(self._q("SELECT 1 FROM blocklist WHERE pattern=? OR resolved_ip=? LIMIT 1", (ip, ip)))

    def is_number_blocked(self, number: str | None) -> bool:
        if not number:
            return False
        return bool(self._q("SELECT 1 FROM blocklist WHERE pattern=? LIMIT 1", (number,)))

    # --- Einstellungen -------------------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        rows = self._q("SELECT v FROM kv WHERE k=?", (key,))
        return rows[0]["v"] if rows else default

    def put(self, key: str, value: str) -> None:
        self._x("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (key, value))

    # --- Aufbewahrung ----------------------------------------------------------
    def purge(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM messages WHERE ts < ? AND peer NOT IN (SELECT peer FROM kept)",
                                 (cutoff,)).rowcount
            self._db.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
            self._db.commit()
            self._db.execute("PRAGMA optimize")
        return n
