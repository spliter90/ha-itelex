"""Abgleich mit anderen Teilnehmerservern und Spiegel des öffentlichen Telefonbuchs.

Zwei Betriebsarten:
- Mit Server-PIN (von den i-Telex-Admins vergeben): vollwertiger Teilnehmerserver. Eigene
  Teilnehmer werden an die Partnerserver gesendet (Sync_Login), deren Verzeichnis täglich komplett
  geholt (Sync_FullQuery). Änderungen gehen über eine Warteschlange stündlich bzw. kurz nach einer
  Änderung raus. Ablauf wie beim offiziellen Server.
- Ohne Server-PIN: das öffentliche Verzeichnis wird regelmäßig über die Suche (Peer_search) gespiegelt
  und dient als Telefonbuch und als Ersatz, wenn die öffentlichen Server nicht erreichbar sind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import tnsproto as T
from exchange import Exchange, split_server
from store import Store

log = logging.getLogger("vermittlung.abgleich")

PUBLISH_EVERY = 300          # eigene Einträge auf Änderungen prüfen (z. B. ablaufende Umleitung)
QUEUE_EVERY = 3600           # Warteschlange spätestens nach einer Stunde senden
QUEUE_DELAY = 20             # nach einer Änderung kurz sammeln, dann senden
FULL_EVERY = 24 * 3600       # Gesamtabfrage bei den Partnerservern
CONNECT_TIMEOUT = 10
LIST_TIMEOUT = 60
KEEP_DELETED_DAYS = 200


class Abgleich:
    def __init__(self, ex: Exchange, store: Store, mirror_hours: float = 6) -> None:
        self.ex = ex
        self.store = store
        self.mirror_every = max(1.0, float(mirror_hours)) * 3600
        self._wake = asyncio.Event()
        self._due_queue = 0.0
        self._force_full = False
        self._force_mirror = False
        self._task: asyncio.Task | None = None
        self.running = ""                       # was gerade läuft (für die Oberfläche)
        ex.on_sync_change = self.soon

    # --- Steuerung ---------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def soon(self) -> None:
        """Nach einer Änderung: in Kürze veröffentlichen und senden."""
        if not self._due_queue:
            self._due_queue = time.time() + QUEUE_DELAY
        self._wake.set()

    def now(self) -> None:
        """Alles sofort: veröffentlichen, senden, Gesamtabfrage bzw. Spiegel erneuern."""
        self._due_queue = time.time()
        self._force_full = True
        self._force_mirror = True
        self._wake.set()

    # --- Zustand -----------------------------------------------------------------
    def _peer_state(self, peer: str) -> dict:
        try:
            return json.loads(self.store.kv_get("peer:" + peer, "{}"))
        except ValueError:
            return {}

    def _set_peer_state(self, peer: str, **kw) -> None:
        st = self._peer_state(peer)
        st.update(kw)
        self.store.kv_set("peer:" + peer, json.dumps(st))

    def status(self) -> dict:
        peers = self.ex.sync_peers()
        counts = self.store.queue_counts()
        mirror = {}
        try:
            mirror = json.loads(self.store.kv_get("spiegel", "{}"))
        except ValueError:
            pass
        return {
            "mode": "abgleich" if self.ex.sync_enabled else "spiegel",
            "peers": [{"server": p, "queue": counts.get(p, 0), **self._peer_state(p)} for p in peers],
            "mirror": mirror,
            "mirror_hours": self.mirror_every / 3600,
            "verzeichnis": self.store.vz_count(),
            "running": self.running,
        }

    # --- Hauptschleife -------------------------------------------------------------
    async def _loop(self) -> None:
        await asyncio.sleep(3)
        last_publish = 0.0
        last_queue = time.time()
        while True:
            try:
                now = time.time()
                if self.ex.sync_enabled:
                    if now - last_publish >= PUBLISH_EVERY or self._due_queue:
                        await self.publish_own()
                        last_publish = now
                    if (self._due_queue and now >= self._due_queue) or now - last_queue >= QUEUE_EVERY:
                        self._due_queue = 0.0
                        last_queue = now
                        await self.send_queues()
                    for peer in self.ex.sync_peers():
                        st = self._peer_state(peer)
                        if self._force_full or now - float(st.get("last_full_try", 0)) >= FULL_EVERY:
                            await self.full_query(peer)
                    self._force_full = False
                else:
                    self._due_queue = 0.0
                # Telefonbuch spiegeln: ohne Abgleich immer, mit Abgleich nur solange er nichts liefert
                if not self.ex.sync_enabled or not self.store.vz_count().get("abgleich"):
                    st = json.loads(self.store.kv_get("spiegel", "{}") or "{}")
                    if self._force_mirror or now - float(st.get("last_try", 0)) >= self.mirror_every:
                        await self.mirror()
                self._force_mirror = False
                self.store.vz_purge_deleted(KEEP_DELETED_DAYS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fehler im Abgleich")
            self._wake.clear()
            wait = 60.0
            if self._due_queue:
                wait = max(1.0, min(wait, self._due_queue - time.time()))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    # --- Eigene Teilnehmer veröffentlichen ------------------------------------------------
    async def publish_own(self) -> int:
        """Eigene Teilnehmer mit dem zuletzt veröffentlichten Stand vergleichen und Änderungen
        (neuer Zeitstempel) in die Warteschlange aller Partnerserver stellen."""
        peers = self.ex.sync_peers()
        if not peers:
            return 0
        changed = 0
        now = int(time.time())
        seen = set()
        for entry in self.store.teilnehmer():
            seen.add(entry["number"])
            rec = await self.ex.published_record(entry)
            if not rec:
                continue                        # z. B. Vermittlung ohne öffentliche Adresse
            fp = T.fingerprint(rec)
            pub = self.store.pub_get(entry["number"])
            if pub and pub["hash"] == fp and not pub["deleted"]:
                continue
            ts = max(now, int(pub["timestamp"]) + 1 if pub else 0)
            self.store.pub_set(entry["number"], fp, ts)
            self.store.queue_add(peers, entry["number"])
            changed += 1
        for pub in self.store.pub_all():
            if pub["number"] not in seen and not pub["deleted"]:
                self.store.pub_set(pub["number"], "", max(now, int(pub["timestamp"]) + 1), deleted=True)
                self.store.queue_add(peers, pub["number"])
                changed += 1
        if changed:
            log.info("%s eigene Einträge für den Abgleich vorgemerkt", changed)
        return changed

    async def record_for_sync(self, number: str) -> dict | None:
        entry = self.store.get(number)
        if entry:
            rec = await self.ex.published_record(entry)
            pub = self.store.pub_get(number)
            if rec and pub:
                rec = dict(rec, timestamp=int(pub["timestamp"]))
            return rec
        pub = self.store.pub_get(number)
        if pub and pub["deleted"]:
            return {"number": number, "name": "", "type": T.T_DELETED, "timestamp": int(pub["timestamp"])}
        vz = self.store.vz_get(number)
        return dict(vz) if vz and vz["quelle"] == "abgleich" else None

    # --- Verbindungen zu anderen Servern ------------------------------------------------------
    async def _connect(self, server: str):
        host, port = split_server(server)
        return await asyncio.wait_for(asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT)

    async def send_queues(self) -> None:
        for peer in self.ex.sync_peers():
            numbers = self.store.queue_for(peer)
            if not numbers:
                continue
            records = [r for r in [await self.record_for_sync(n) for n in numbers] if r]
            self.running = f"Sende {len(records)} Einträge an {peer}"
            try:
                reader, writer = await self._connect(peer)
                try:
                    writer.write(T.pkt_sync(T.SYNC_LOGIN, self.ex.cfg.server_pin))
                    await writer.drain()
                    await T.send_list(reader, writer, records, with_pin=True, timeout=LIST_TIMEOUT,
                                      wait_first_ack=True)
                    # Die Gegenseite schließt, sobald sie die Liste übernommen hat
                    try:
                        await asyncio.wait_for(reader.read(64), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                finally:
                    writer.close()
                self.store.queue_done(peer, numbers)
                self._set_peer_state(peer, last_send=time.time(), last_send_count=len(records), error="")
                self.store.log("abgleich", ip=peer, detail=f"{len(records)} Einträge gesendet")
                log.info("%s Einträge an %s gesendet", len(records), peer)
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
                self._set_peer_state(peer, error=f"Senden: {e or type(e).__name__}", error_ts=time.time())
                self.store.log("abgleich", ip=peer, ok=False, detail=f"Senden fehlgeschlagen: {e}")
                log.warning("Abgleich mit %s: Senden fehlgeschlagen: %s", peer, e)
            finally:
                self.running = ""

    async def full_query(self, peer: str) -> int:
        self._set_peer_state(peer, last_full_try=time.time())
        self.running = f"Gesamtabfrage bei {peer}"
        try:
            reader, writer = await self._connect(peer)
            try:
                writer.write(T.pkt_sync(T.SYNC_FULLQUERY, self.ex.cfg.server_pin))
                await writer.drain()
                records = await T.receive_list(reader, writer, timeout=LIST_TIMEOUT)
            finally:
                writer.close()
            n = self.ex.merge(records, von=peer)
            self._set_peer_state(peer, last_full=time.time(), last_full_count=len(records), error="")
            self.store.log("abgleich", ip=peer, detail=f"Gesamtabfrage: {len(records)} Einträge, {n} neu oder geändert")
            log.info("Gesamtabfrage bei %s: %s Einträge, %s neu oder geändert", peer, len(records), n)
            return n
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
            msg = str(e) or type(e).__name__
            if isinstance(e, asyncio.IncompleteReadError) and not e.partial:
                msg = "Verbindung getrennt (Server-PIN falsch oder nicht freigeschaltet?)"
            self._set_peer_state(peer, error=f"Gesamtabfrage: {msg}", error_ts=time.time())
            self.store.log("abgleich", ip=peer, ok=False, detail=f"Gesamtabfrage fehlgeschlagen: {msg}")
            log.warning("Gesamtabfrage bei %s fehlgeschlagen: %s", peer, msg)
            return 0
        finally:
            self.running = ""

    async def mirror(self) -> int:
        """Öffentliches Verzeichnis über die Suche holen (ein Leerzeichen liefert alle Einträge)."""
        st = {"last_try": time.time()}
        try:
            st.update(json.loads(self.store.kv_get("spiegel", "{}") or "{}"), last_try=time.time())
        except ValueError:
            pass
        self.store.kv_set("spiegel", json.dumps(st))
        for server in self.ex.cfg.upstream:
            self.running = f"Telefonbuch von {server}"
            try:
                reader, writer = await self._connect(server)
                try:
                    writer.write(T.pkt_search(" "))
                    await writer.drain()
                    records = await T.receive_list(reader, writer, timeout=LIST_TIMEOUT)
                finally:
                    writer.close()
                records = [r for r in records if r["type"] != T.T_DELETED]
                if not records:
                    raise ConnectionError("keine Einträge erhalten")
                n = self.store.vz_replace_mirror(records, server)
                st.update(last_ok=time.time(), count=n, server=server, error="")
                self.store.kv_set("spiegel", json.dumps(st))
                self.store.log("abgleich", ip=server, detail=f"Telefonbuch: {n} öffentliche Einträge")
                log.info("Telefonbuch von %s: %s öffentliche Einträge", server, n)
                return n
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
                st.update(error=f"{server}: {e or type(e).__name__}")
                log.warning("Telefonbuch von %s nicht abrufbar: %s", server, e)
            finally:
                self.running = ""
        self.store.kv_set("spiegel", json.dumps(st))
        self.store.log("abgleich", ok=False, detail="Telefonbuch: kein öffentlicher Server erreichbar")
        return 0
