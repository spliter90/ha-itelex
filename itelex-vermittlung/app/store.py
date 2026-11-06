"""SQLite-Speicher der Vermittlung: Teilnehmer, Verzeichnis, Abgleich und Aktivitätsprotokoll."""

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
-- Verzeichnis des i-Telex-Netzes: vom Abgleich mit anderen Teilnehmerservern (quelle 'abgleich')
-- oder aus der öffentlichen Suche gespiegelt (quelle 'suche'). Enthält keine eigenen Teilnehmer.
CREATE TABLE IF NOT EXISTS verzeichnis (
    number TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    type INTEGER NOT NULL DEFAULT 0,
    hostname TEXT DEFAULT '',
    ipaddress TEXT DEFAULT '',
    port INTEGER DEFAULT 0,
    extension INTEGER DEFAULT 0,   -- Rohwert wie im Paket (0..110)
    pin INTEGER DEFAULT 0,
    disabled INTEGER DEFAULT 0,
    timestamp INTEGER DEFAULT 0,   -- Unix-Sekunden, Stand des Eintrags im Netz
    quelle TEXT DEFAULT '',
    von TEXT DEFAULT '',           -- Server, von dem der Stand kam
    seen REAL DEFAULT 0
);
-- Was von den eigenen Teilnehmern zuletzt ins Netz gegeben wurde
CREATE TABLE IF NOT EXISTS veroeffentlicht (
    number TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    deleted INTEGER DEFAULT 0
);
-- Einträge, die noch an andere Teilnehmerserver gesendet werden müssen
-- Rundsenden: Gruppen (die Nummer steht als Teilnehmer mit mode 'gruppe' in "teilnehmer")
CREATE TABLE IF NOT EXISTS gruppen (
    number TEXT PRIMARY KEY,
    kennung TEXT NOT NULL DEFAULT '',
    berechtigung TEXT NOT NULL DEFAULT 'mitglieder',   -- mitglieder / liste / alle
    erlaubt TEXT DEFAULT '',                           -- Nummern, Komma-getrennt (bei 'liste')
    selbst INTEGER DEFAULT 0,                          -- An-/Abmelden per Telex erlaubt
    kopf INTEGER DEFAULT 1                             -- Kopfzeile mit Gruppe und Absender
);
CREATE TABLE IF NOT EXISTS mitglieder (
    gruppe TEXT NOT NULL,
    number TEXT NOT NULL,
    name TEXT DEFAULT '',
    quelle TEXT DEFAULT 'verwaltung',                  -- verwaltung / telex
    since REAL NOT NULL,
    PRIMARY KEY (gruppe, number)
);
CREATE TABLE IF NOT EXISTS rundsendungen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gruppe TEXT NOT NULL,
    ts REAL NOT NULL,
    absender TEXT DEFAULT '',
    absender_kennung TEXT DEFAULT '',
    quelle TEXT DEFAULT 'telex',                       -- telex / verwaltung
    text TEXT NOT NULL,
    status TEXT DEFAULT 'laeuft',                      -- laeuft / fertig
    bericht TEXT DEFAULT ''                            -- Zustellbericht an den Absender
);
CREATE TABLE IF NOT EXISTS zustellungen (
    rid INTEGER NOT NULL,
    number TEXT NOT NULL,
    name TEXT DEFAULT '',
    status TEXT DEFAULT 'offen',                       -- offen / wartet / zugestellt / fehler
    detail TEXT DEFAULT '',
    answerback TEXT DEFAULT '',
    tries INTEGER DEFAULT 0,
    next_try REAL DEFAULT 0,
    ts REAL DEFAULT 0,
    PRIMARY KEY (rid, number)
);
-- Anrufbare Dienste (Plugins): Nummer steht als Teilnehmer mit mode 'dienst' in "teilnehmer"
CREATE TABLE IF NOT EXISTS dienste (
    number TEXT PRIMARY KEY,
    plugin TEXT NOT NULL,
    kennung TEXT NOT NULL DEFAULT ''
);
-- Gespeicherter Stand je Plugin und Anrufer (z. B. Spielstand), JSON
CREATE TABLE IF NOT EXISTS dienst_daten (
    plugin TEXT NOT NULL,
    anrufer TEXT NOT NULL,
    daten TEXT NOT NULL DEFAULT '{}',
    ts REAL NOT NULL,
    PRIMARY KEY (plugin, anrufer)
);
CREATE TABLE IF NOT EXISTS warteschlange (
    server TEXT NOT NULL,
    number TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (server, number)
);
"""

VZ_FIELDS = ("number", "name", "type", "hostname", "ipaddress", "port", "extension", "pin",
             "disabled", "timestamp")

MODES = ("direkt", "dynamisch", "vermittlung", "gruppe", "dienst")
VX_MODES = ("vermittlung", "gruppe", "dienst")          # erreichbar über eine Durchwahl der Vermittlung
GRUPPE_RECHTE = ("mitglieder", "liste", "alle")


def _numbers(value) -> list[str]:
    if isinstance(value, str):
        value = value.replace(";", ",").replace(" ", ",").split(",")
    out = []
    for v in value or []:
        v = str(v).strip()
        if v and v not in out:
            if not v.isdigit() or not 3 <= len(v) <= 9:
                raise ValueError(f"Ungültige Nummer: {v}")
            out.append(v)
    return out


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(teilnehmer)")}
            if "umleitung" not in cols:
                self._db.execute("ALTER TABLE teilnehmer ADD COLUMN umleitung TEXT DEFAULT ''")
            if "umleitung_bis" not in cols:
                self._db.execute("ALTER TABLE teilnehmer ADD COLUMN umleitung_bis REAL DEFAULT 0")
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
        rows = self._q("SELECT * FROM teilnehmer WHERE ext_code=? AND mode IN ('vermittlung','gruppe','dienst')",
                       (code,))
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
        if "umleitung" in data:
            umleitung = str(data.get("umleitung", "") or "").strip()
            try:
                umleitung_bis = float(data.get("umleitung_bis") or 0) if umleitung else 0.0
            except (TypeError, ValueError) as e:
                raise ValueError("Umleitung bis: ungültige Zeit") from e
        else:                                   # nicht mitgeschickt: bestehende Umleitung behalten
            umleitung = (old or {}).get("umleitung") or ""
            umleitung_bis = float((old or {}).get("umleitung_bis") or 0)
        if umleitung and (not umleitung.isdigit() or not 3 <= len(umleitung) <= 9
                          or int(umleitung) > 0xFFFFFFFF):
            raise ValueError("Umleitung: Zielnummer mit 3 bis 9 Ziffern")
        if umleitung == number:
            raise ValueError("Eine Nummer kann nicht auf sich selbst umgeleitet werden")
        if original and original != number and self.get(number):
            raise ValueError(f"Nummer {number} ist schon vergeben")
        if not original and old:
            raise ValueError(f"Nummer {number} ist schon vergeben")
        pin = int(old["pin"]) if old and old["pin"] else random.randint(1000, 65535)
        if data.get("new_pin"):
            pin = random.randint(1000, 65535)
        ext_code = (old or {}).get("ext_code") or 0
        if mode in VX_MODES and not ext_code:
            ext_code = self._free_ext_code()
        if mode not in VX_MODES:
            ext_code = 0
        if mode == "dynamisch" and old and old["mode"] == "dynamisch" and not host:
            host = old["host"]
        now = time.time()
        row = (number, name, mode, host if mode not in VX_MODES else "", port, ext,
               1 if data.get("ascii") else 0, pin, ext_code, str(data.get("note", "")).strip()[:200],
               0 if data.get("enabled") is False else 1, now, (old or {}).get("created") or now,
               umleitung, umleitung_bis)
        with self._lock:
            if old:
                self._db.execute("DELETE FROM teilnehmer WHERE number=?", (old["number"],))
            self._db.execute("INSERT INTO teilnehmer(number,name,mode,host,port,extension,ascii,pin,ext_code,"
                             "note,enabled,updated,created,umleitung,umleitung_bis) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self._db.commit()
        return self.get(number)

    def delete(self, number: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM teilnehmer WHERE number=?", (number,))
            self._db.execute("DELETE FROM gruppen WHERE number=?", (number,))
            self._db.execute("DELETE FROM mitglieder WHERE gruppe=?", (number,))
            self._db.execute("DELETE FROM dienste WHERE number=?", (number,))
            self._db.commit()

    # --- Anrufbare Dienste ---------------------------------------------------------
    def dienst(self, number: str) -> dict | None:
        rows = self._q("SELECT t.*, d.plugin, d.kennung FROM teilnehmer t JOIN dienste d ON d.number=t.number "
                       "WHERE t.number=?", (str(number),))
        return rows[0] if rows else None

    def dienste(self) -> list[dict]:
        return self._q("SELECT t.*, d.plugin, d.kennung FROM teilnehmer t JOIN dienste d ON d.number=t.number "
                       "ORDER BY CAST(t.number AS INTEGER)")

    def dienst_save(self, data: dict, plugins: set[str], original: str | None = None) -> dict:
        if original and not self.dienst(original):
            raise ValueError("Unbekannter Dienst")
        plugin = str(data.get("plugin", ""))
        if plugin not in plugins:
            raise ValueError("Unbekanntes Plugin")
        number = str(data.get("number", "")).strip()
        kennung = str(data.get("kennung", "") or "").strip() or f"{number} {data.get('name', '')}"[:20].strip()
        if len(kennung) > 24:
            raise ValueError("Kennung höchstens 24 Zeichen")
        entry = self.save({"number": number, "name": data.get("name", ""), "mode": "dienst",
                           "enabled": data.get("enabled", True) is not False, "umleitung": ""}, original=original)
        with self._lock:
            if original and original != number:
                self._db.execute("DELETE FROM dienste WHERE number=?", (original,))
            self._db.execute("INSERT OR REPLACE INTO dienste(number,plugin,kennung) VALUES(?,?,?)",
                             (entry["number"], plugin, kennung))
            self._db.commit()
        return self.dienst(entry["number"])

    def daten_get(self, plugin: str, anrufer: str) -> dict:
        import json
        rows = self._q("SELECT daten FROM dienst_daten WHERE plugin=? AND anrufer=?", (plugin, anrufer))
        try:
            return json.loads(rows[0]["daten"]) if rows else {}
        except ValueError:
            return {}

    def daten_set(self, plugin: str, anrufer: str, daten: dict) -> None:
        import json
        self._x("INSERT OR REPLACE INTO dienst_daten(plugin,anrufer,daten,ts) VALUES(?,?,?,?)",
                (plugin, anrufer, json.dumps(daten, ensure_ascii=False), time.time()))

    def daten_alle(self, plugin: str) -> list[dict]:
        import json
        out = []
        for r in self._q("SELECT * FROM dienst_daten WHERE plugin=? ORDER BY ts DESC", (plugin,)):
            try:
                r["daten"] = json.loads(r["daten"])
            except ValueError:
                r["daten"] = {}
            out.append(r)
        return out

    # --- Rundsenden: Gruppen ---------------------------------------------------
    def gruppe(self, number: str) -> dict | None:
        rows = self._q("SELECT t.*, g.kennung, g.berechtigung, g.erlaubt, g.selbst, g.kopf, "
                       "(SELECT COUNT(*) FROM mitglieder m WHERE m.gruppe=t.number) AS anzahl "
                       "FROM teilnehmer t JOIN gruppen g ON g.number=t.number WHERE t.number=?", (str(number),))
        return rows[0] if rows else None

    def gruppen(self) -> list[dict]:
        return self._q("SELECT t.*, g.kennung, g.berechtigung, g.erlaubt, g.selbst, g.kopf, "
                       "(SELECT COUNT(*) FROM mitglieder m WHERE m.gruppe=t.number) AS anzahl "
                       "FROM teilnehmer t JOIN gruppen g ON g.number=t.number "
                       "ORDER BY CAST(t.number AS INTEGER)")

    def gruppe_save(self, data: dict, original: str | None = None) -> dict:
        if original and not self.gruppe(original):
            raise ValueError("Unbekannte Gruppe")
        recht = str(data.get("berechtigung", "mitglieder"))
        if recht not in GRUPPE_RECHTE:
            raise ValueError("Unbekannte Berechtigung")
        erlaubt = ",".join(_numbers(data.get("erlaubt", "")))
        number = str(data.get("number", "")).strip()
        kennung = str(data.get("kennung", "") or "").strip()
        if not kennung:
            kennung = f"{number} {str(data.get('name', '')).strip()}"[:20].strip()
        if len(kennung) > 24:
            raise ValueError("Kennung höchstens 24 Zeichen")
        entry = self.save({"number": number, "name": data.get("name", ""), "mode": "gruppe",
                           "note": data.get("note", ""), "enabled": data.get("enabled", True) is not False,
                           "umleitung": ""}, original=original)
        with self._lock:
            if original and original != number:
                self._db.execute("DELETE FROM gruppen WHERE number=?", (original,))
                self._db.execute("UPDATE mitglieder SET gruppe=? WHERE gruppe=?", (number, original))
            self._db.execute("INSERT OR REPLACE INTO gruppen(number,kennung,berechtigung,erlaubt,selbst,kopf) "
                             "VALUES(?,?,?,?,?,?)",
                             (entry["number"], kennung, recht, erlaubt, 1 if data.get("selbst") else 0,
                              0 if data.get("kopf") is False else 1))
            self._db.commit()
        return self.gruppe(entry["number"])

    def mitglieder(self, gruppe: str) -> list[dict]:
        return self._q("SELECT * FROM mitglieder WHERE gruppe=? ORDER BY CAST(number AS INTEGER)", (gruppe,))

    def ist_mitglied(self, gruppe: str, number: str) -> bool:
        return bool(self._q("SELECT 1 FROM mitglieder WHERE gruppe=? AND number=?", (gruppe, number)))

    def mitglied_add(self, gruppe: str, number: str, name: str = "", quelle: str = "verwaltung") -> None:
        _numbers([number])
        if number == gruppe:
            raise ValueError("Die Gruppe kann nicht ihr eigenes Mitglied sein")
        if self.gruppe(number):
            raise ValueError("Gruppen können nicht Mitglied einer Gruppe sein")
        self._x("INSERT OR REPLACE INTO mitglieder(gruppe,number,name,quelle,since) VALUES(?,?,?,?,?)",
                (gruppe, number, name[:40], quelle, time.time()))

    def mitglied_del(self, gruppe: str, number: str) -> bool:
        return self._x("DELETE FROM mitglieder WHERE gruppe=? AND number=?", (gruppe, number)) > 0

    # --- Rundsenden: Sendungen und Zustellungen --------------------------------------
    def rs_create(self, gruppe: str, text: str, empfaenger: list[dict], *, absender: str = "",
                  kennung: str = "", quelle: str = "telex") -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute("INSERT INTO rundsendungen(gruppe,ts,absender,absender_kennung,quelle,text) "
                                   "VALUES(?,?,?,?,?,?)", (gruppe, now, absender, kennung, quelle, text))
            rid = cur.lastrowid
            self._db.executemany("INSERT INTO zustellungen(rid,number,name,ts) VALUES(?,?,?,?)",
                                 [(rid, m["number"], m.get("name") or "", now) for m in empfaenger])
            self._db.commit()
        return rid

    def rs_get(self, rid: int) -> dict | None:
        rows = self._q("SELECT * FROM rundsendungen WHERE id=?", (rid,))
        if not rows:
            return None
        r = rows[0]
        r["zustellungen"] = self._q("SELECT * FROM zustellungen WHERE rid=? ORDER BY CAST(number AS INTEGER)",
                                    (rid,))
        return r

    def rs_list(self, limit: int = 50, gruppe: str = "") -> list[dict]:
        where, args = ("WHERE r.gruppe=?", (gruppe,)) if gruppe else ("", ())
        return self._q(f"SELECT r.*, (SELECT COUNT(*) FROM zustellungen z WHERE z.rid=r.id) AS total, "
                       f"(SELECT COUNT(*) FROM zustellungen z WHERE z.rid=r.id AND z.status='zugestellt') AS ok, "
                       f"(SELECT COUNT(*) FROM zustellungen z WHERE z.rid=r.id AND z.status='fehler') AS failed "
                       f"FROM rundsendungen r {where} ORDER BY r.ts DESC LIMIT ?", args + (limit,))

    def rs_open(self) -> list[int]:
        return [r["id"] for r in self._q("SELECT id FROM rundsendungen WHERE status='laeuft' ORDER BY id")]

    def rs_update(self, rid: int, **fields) -> None:
        if fields:
            self._x(f"UPDATE rundsendungen SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                    tuple(fields.values()) + (rid,))

    def zu_update(self, rid: int, number: str, **fields) -> None:
        fields["ts"] = time.time()
        self._x(f"UPDATE zustellungen SET {','.join(k + '=?' for k in fields)} WHERE rid=? AND number=?",
                tuple(fields.values()) + (rid, number))

    def rs_purge(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        with self._lock:
            self._db.execute("DELETE FROM zustellungen WHERE rid IN "
                             "(SELECT id FROM rundsendungen WHERE ts<? AND status='fertig')", (cutoff,))
            self._db.execute("DELETE FROM rundsendungen WHERE ts<? AND status='fertig'", (cutoff,))
            self._db.commit()

    def set_dynamic_ip(self, number: str, ip: str, port: int) -> None:
        self._x("UPDATE teilnehmer SET host=?, port=?, updated=? WHERE number=?",
                (ip, port, time.time(), number))

    def set_umleitung(self, number: str, ziel: str, bis: float = 0) -> None:
        self._x("UPDATE teilnehmer SET umleitung=?, umleitung_bis=?, updated=? WHERE number=?",
                (ziel, bis if ziel else 0, time.time(), number))

    # --- Verzeichnis des Netzes ------------------------------------------------
    def vz_get(self, number: str) -> dict | None:
        rows = self._q("SELECT * FROM verzeichnis WHERE number=?", (str(number),))
        return rows[0] if rows else None

    def vz_put(self, rec: dict, quelle: str, von: str = "") -> None:
        vals = tuple(rec.get(k) or (0 if k not in ("number", "name", "hostname", "ipaddress") else "")
                     for k in VZ_FIELDS)
        self._x(f"INSERT OR REPLACE INTO verzeichnis({','.join(VZ_FIELDS)},quelle,von,seen) "
                f"VALUES({','.join('?' * len(VZ_FIELDS))},?,?,?)", vals + (quelle, von, time.time()))

    def vz_replace_mirror(self, records: list[dict], von: str) -> int:
        """Gespiegeltes öffentliches Verzeichnis komplett ersetzen (nur Einträge aus der Suche)."""
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM verzeichnis WHERE quelle='suche'")
            for rec in records:
                vals = tuple(rec.get(k) or (0 if k not in ("number", "name", "hostname", "ipaddress") else "")
                             for k in VZ_FIELDS)
                self._db.execute(f"INSERT OR IGNORE INTO verzeichnis({','.join(VZ_FIELDS)},quelle,von,seen) "
                                 f"VALUES({','.join('?' * len(VZ_FIELDS))},'suche',?,?)", vals + (von, now))
            self._db.commit()
        return len(records)

    def vz_all(self) -> list[dict]:
        return self._q("SELECT * FROM verzeichnis ORDER BY CAST(number AS INTEGER)")

    def vz_count(self) -> dict:
        rows = self._q("SELECT quelle, COUNT(*) AS n FROM verzeichnis WHERE type!=0 GROUP BY quelle")
        return {r["quelle"]: r["n"] for r in rows}

    def vz_purge_deleted(self, days: int = 200) -> int:
        return self._x("DELETE FROM verzeichnis WHERE type=0 AND timestamp<?", (time.time() - days * 86400,))

    def vz_set_ip(self, number: str, ip: str, port: int, pin: int, ts: int) -> None:
        self._x("UPDATE verzeichnis SET ipaddress=?, port=?, pin=?, timestamp=?, seen=? WHERE number=?",
                (ip, port, pin, ts, time.time(), number))

    # --- Veröffentlichung eigener Teilnehmer -----------------------------------
    def pub_get(self, number: str) -> dict | None:
        rows = self._q("SELECT * FROM veroeffentlicht WHERE number=?", (str(number),))
        return rows[0] if rows else None

    def pub_all(self) -> list[dict]:
        return self._q("SELECT * FROM veroeffentlicht")

    def pub_set(self, number: str, hash_: str, ts: int, deleted: bool = False) -> None:
        self._x("INSERT OR REPLACE INTO veroeffentlicht(number,hash,timestamp,deleted) VALUES(?,?,?,?)",
                (number, hash_, ts, 1 if deleted else 0))

    def pub_delete(self, number: str) -> None:
        self._x("DELETE FROM veroeffentlicht WHERE number=?", (number,))

    # --- Warteschlange für den Abgleich ----------------------------------------
    def queue_add(self, servers: list[str], number: str) -> None:
        now = time.time()
        with self._lock:
            for srv in servers:
                self._db.execute("INSERT OR REPLACE INTO warteschlange(server,number,ts) VALUES(?,?,?)",
                                 (srv, number, now))
            self._db.commit()

    def queue_for(self, server: str) -> list[str]:
        return [r["number"] for r in self._q("SELECT number FROM warteschlange WHERE server=? ORDER BY ts",
                                             (server,))]

    def queue_done(self, server: str, numbers: list[str]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM warteschlange WHERE server=? AND number=?",
                                 [(server, n) for n in numbers])
            self._db.commit()

    def queue_counts(self) -> dict:
        return {r["server"]: r["n"] for r in self._q(
            "SELECT server, COUNT(*) AS n FROM warteschlange GROUP BY server")}

    def queue_prune(self, servers: list[str]) -> None:
        if servers:
            self._x(f"DELETE FROM warteschlange WHERE server NOT IN ({','.join('?' * len(servers))})",
                    tuple(servers))
        else:
            self._x("DELETE FROM warteschlange")

    # --- Einstellungen / Zustand -----------------------------------------------
    def kv_get(self, key: str, default: str = "") -> str:
        rows = self._q("SELECT v FROM kv WHERE k=?", (key,))
        return rows[0]["v"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self._x("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (key, value))

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
