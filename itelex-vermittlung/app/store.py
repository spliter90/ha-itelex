"""SQLite-Speicher der Vermittlung: Teilnehmer und Aktivitätsprotokoll."""

from __future__ import annotations

import random
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS teilnehmer (
    number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,            -- direkt / dynamisch / vermittlung
    host TEXT DEFAULT '',          -- Hostname oder IP (direkt); zuletzt gemeldete IP (dynamisch)
    port INTEGER DEFAULT 134,
    extension TEXT DEFAULT '',     -- Durchwahl beim Ziel (nur direkt/dynamisch)
    ascii INTEGER DEFAULT 0,
    pin INTEGER DEFAULT 0,
    ext_code INTEGER DEFAULT 0,    -- interne Durchwahl der Vermittlung (nur vermittlung)
    note TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    updated REAL NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,            -- abfrage / meldung / anmeldung / anruf / fehler
    number TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    ok INTEGER DEFAULT 1,
    detail TEXT DEFAULT '',
    duration REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

MODES = ("direkt", "dynamisch", "vermittlung")


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql, args).fetchall()]
            self._db.commit()
            return rows

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid or cur.rowcount

    # --- Teilnehmer ----------------------------------------------------------
    def teilnehmer(self) -> list[dict]:
        return self._q("SELECT * FROM teilnehmer ORDER BY CAST(number AS INTEGER)")

    def get(self, number: str) -> dict | None:
        rows = self._q("SELECT * FROM teilnehmer WHERE number=?", (str(number),))
        return rows[0] if rows else None

    def by_ext_code(self, code: int) -> dict | None:
        rows = self._q("SELECT * FROM teilnehmer WHERE ext_code=? AND mode='vermittlung'", (code,))
        return rows[0] if rows else None

    def _free_ext_code(self) -> int:
        used = {r["ext_code"] for r in self._q("SELECT ext_code FROM teilnehmer WHERE ext_code>0")}
        for code in range(1, 100):
            if code not in used:
                return code
        raise ValueError("Alle 99 Plätze der Vermittlung sind belegt")

    def save(self, data: dict, original: str | None = None) -> dict:
        number = str(data.get("number", "")).strip()
        name = str(data.get("name", "")).strip()
        mode = str(data.get("mode", "direkt"))
        if not number.isdigit() or not 3 <= len(number) <= 9 or int(number) > 0xFFFFFFFF:
            raise ValueError("Nummer: 3 bis 9 Ziffern")
        if not name:
            raise ValueError("Bitte einen Namen angeben")
        if len(name) > 40:
            raise ValueError("Name höchstens 40 Zeichen")
        if mode not in MODES:
            raise ValueError("Unbekannte Anbindung")
        host = str(data.get("host", "")).strip()
        if mode == "direkt" and not host:
            raise ValueError("Bei direkter Anbindung Hostname oder IP angeben")
        if len(host) > 40:
            raise ValueError("Hostname höchstens 40 Zeichen")
        try:
            port = int(data.get("port") or 134)
        except ValueError as e:
            raise ValueError("Port muss eine Zahl sein") from e
        if not 1 <= port <= 65535:
            raise ValueError("Port zwischen 1 und 65535")
        ext = str(data.get("extension", "") or "").strip()
        if ext and (not ext.isdigit() or len(ext) > 2):
            raise ValueError("Durchwahl: eine oder zwei Ziffern")
        old = self.get(original or number)
        if original and original != number and self.get(number):
            raise ValueError(f"Nummer {number} ist schon vergeben")
        if not original and old:
            raise ValueError(f"Nummer {number} ist schon vergeben")
        pin = int(old["pin"]) if old and old["pin"] else random.randint(1000, 65535)
        if data.get("new_pin"):
            pin = random.randint(1000, 65535)
        ext_code = (old or {}).get("ext_code") or 0
        if mode == "vermittlung" and not ext_code:
            ext_code = self._free_ext_code()
        if mode != "vermittlung":
            ext_code = 0
        if mode == "dynamisch" and old and old["mode"] == "dynamisch" and not host:
            host = old["host"]
        now = time.time()
        row = (number, name, mode, host if mode != "vermittlung" else "", port, ext,
               1 if data.get("ascii") else 0, pin, ext_code, str(data.get("note", "")).strip()[:200],
               0 if data.get("enabled") is False else 1, now, (old or {}).get("created") or now)
        with self._lock:
            if old:
                self._db.execute("DELETE FROM teilnehmer WHERE number=?", (old["number"],))
            self._db.execute("INSERT INTO teilnehmer(number,name,mode,host,port,extension,ascii,pin,ext_code,"
                             "note,enabled,updated,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self._db.commit()
        return self.get(number)

    def delete(self, number: str) -> None:
        self._x("DELETE FROM teilnehmer WHERE number=?", (number,))

    def set_dynamic_ip(self, number: str, ip: str, port: int) -> None:
        self._x("UPDATE teilnehmer SET host=?, port=?, updated=? WHERE number=?",
                (ip, port, time.time(), number))

    # --- Protokoll -----------------------------------------------------------
    def log(self, kind: str, *, number: str = "", ip: str = "", ok: bool = True, detail: str = "",
            duration: float = 0) -> None:
        self._x("INSERT INTO events(ts,kind,number,ip,ok,detail,duration) VALUES(?,?,?,?,?,?,?)",
                (time.time(), kind, number, ip, 1 if ok else 0, detail[:300], duration))

    def events(self, limit: int = 300, kind: str = "") -> list[dict]:
        if kind:
            return self._q("SELECT * FROM events WHERE kind=? ORDER BY ts DESC LIMIT ?", (kind, limit))
        return self._q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))

    def stats(self, since: float) -> dict:
        rows = self._q("SELECT kind, COUNT(*) AS n FROM events WHERE ts>? GROUP BY kind", (since,))
        return {r["kind"]: r["n"] for r in rows}

    def purge(self, days: int) -> int:
        return self._x("DELETE FROM events WHERE ts<?", (time.time() - days * 86400,))
