"""i-Telex Protokoll: Baudot-Kodierung (ITA2) und Paketformat.

Paket = [Typ][Länge][Daten...]. Auf der i-Telex-Leitung stehen die 5 Bit jedes Baudot-Zeichens
in umgekehrter Reihenfolge zur Tabelle (wie piTelex: BaudotMurrayCode(flip_bits=True)).
"""

from __future__ import annotations

import asyncio

# Pakettypen
HEARTBEAT = 0x00
DIRECT_DIAL = 0x01
BAUDOT = 0x02
END = 0x03
REJECT = 0x04
ACK = 0x06
VERSION = 0x07
SELFTEST = 0x08
REMOTE_CONFIG = 0x09

PROTOCOL_VERSION = 1
SOFTWARE_ID = "ha01"  # max. 6 Zeichen, wird im Versionspaket übertragen

REJECT_REASONS = {
    "occ": "Gegenstelle besetzt",
    "abs": "Anschluss abgeschaltet",
    "nc": "keine Verbindung",
    "der": "Fernschreiber startet nicht",
    "na": "Durchwahl nicht erlaubt",
    "ver": "Protokollversion inkompatibel",
}

# Interne Steuerzeichen
WRU = "\x05"   # Wer da? (Zi + D)
BELL = "\x07"  # Klingel (Zi + J)

_LTRS_CODE = 0x1F
_FIGS_CODE = 0x1B

# Index = Baudot-Wert (LSB zuerst). None = Umschaltung / nicht belegt.
_LETTERS = [
    None, "E", "\n", "A", " ", "S", "I", "U",
    "\r", "D", "R", "J", "N", "F", "C", "K",
    "T", "Z", "L", "W", "H", "Y", "P", "Q",
    "O", "B", "G", None, "M", "X", "V", None,
]
_FIGURES = [
    None, "3", "\n", "-", " ", "'", "8", "7",
    "\r", WRU, "4", BELL, ",", None, ":", "(",
    "5", "+", ")", "2", None, "6", "0", "1",
    "9", "?", None, None, ".", "/", "=", None,
]

def _flip5(b: int) -> int:
    b &= 0x1F
    return int(f"{b:05b}"[::-1], 2)


# Tabellenwert <-> Wert auf der i-Telex-Leitung (Bitreihenfolge gespiegelt; die Umschaltzeichen
# 0x1F und 0x1B sind symmetrisch und bleiben gleich)
_WIRE = bytes(_flip5(i) for i in range(32))

_ENC_LETTERS = {c: i for i, c in enumerate(_LETTERS) if c}
_ENC_FIGURES = {c: i for i, c in enumerate(_FIGURES) if c}
_BOTH = {" ", "\r", "\n"}

_REPLACE = {
    "Ä": "AE", "Ö": "OE", "Ü": "UE", "ß": "SS", "ẞ": "SS",
    "\t": " ", "!": ".", '"': "'", ";": ",", "_": "-", "*": "X",
    "@": "(AT)", "&": "+", "%": "(PROZENT)", "#": "(NR)", "€": "EUR",
    "[": "(", "]": ")", "{": "(", "}": ")", "<": "(", ">": ")",
}


def normalize(text: str) -> str:
    """Text auf den Fernschreib-Zeichenvorrat abbilden (Großschrift, Umlaute)."""
    out = []
    for ch in text.upper():
        if ch in _REPLACE:
            out.append(_REPLACE[ch])
        elif ch in _ENC_LETTERS or ch in _ENC_FIGURES or ch in (WRU, BELL):
            out.append(ch)
        elif ch == "\r":
            continue
        else:
            out.append("?")
    return "".join(out)


def wrap(text: str, width: int = 68) -> str:
    """Zeilen auf Fernschreiberbreite umbrechen. Zeilenende wird CR LF."""
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            while len(word) > width:
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return "\r\n".join(lines)


class BaudotEncoder:
    def __init__(self) -> None:
        self.mode: str | None = None  # "L" / "F"

    def encode(self, text: str) -> bytes:
        return bytes(_WIRE[b] for b in self._encode(text))

    def _encode(self, text: str) -> bytes:
        out = bytearray()
        for ch in text:
            if ch in _BOTH:
                out.append(_ENC_LETTERS[ch])
            elif ch in _ENC_LETTERS:
                if self.mode != "L":
                    out.append(_LTRS_CODE)
                    self.mode = "L"
                out.append(_ENC_LETTERS[ch])
            elif ch in _ENC_FIGURES:
                if self.mode != "F":
                    out.append(_FIGS_CODE)
                    self.mode = "F"
                out.append(_ENC_FIGURES[ch])
        return bytes(out)

    def force_letters(self) -> bytes:
        self.mode = "L"
        return bytes([_WIRE[_LTRS_CODE]])


class BaudotDecoder:
    def __init__(self) -> None:
        self.mode = "L"

    def decode(self, data: bytes) -> str:
        out = []
        for b in data:
            b = _WIRE[b & 0x1F]
            if b == _LTRS_CODE:
                self.mode = "L"
            elif b == _FIGS_CODE:
                self.mode = "F"
            elif b == 0:
                continue
            else:
                table = _LETTERS if self.mode == "L" else _FIGURES
                ch = table[b]
                if ch is not None:
                    out.append(ch)
        return "".join(out)


def to_display(text: str) -> str:
    """Empfangenen Text für die Anzeige aufbereiten."""
    text = text.replace("\r\n", "\n").replace("\n\r", "\n").replace("\r", "\n")
    text = text.replace(WRU, "✠").replace(BELL, "\U0001F514")
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip("\n ")


# --- Pakete -----------------------------------------------------------------

def pkt(ptype: int, payload: bytes = b"") -> bytes:
    return bytes([ptype, len(payload)]) + payload


def pkt_version() -> bytes:
    sw = SOFTWARE_ID.encode("ascii")[:6]
    payload = bytes([PROTOCOL_VERSION]) + sw
    if len(sw) < 6:
        payload += b"\x00"
    return pkt(VERSION, payload)


def pkt_ack(counter: int) -> bytes:
    return pkt(ACK, bytes([counter & 0xFF]))


def pkt_reject(reason: str) -> bytes:
    return pkt(REJECT, reason.encode("ascii"))


def encode_extension(ext: str | None) -> int:
    if not ext:
        return 0
    try:
        n = int(ext)
    except ValueError:
        return 0
    if len(ext) == 1:
        return 110 if n == 0 else n + 100
    if len(ext) == 2:
        return 100 if n == 0 else n
    return 0


def decode_extension(value: int) -> str | None:
    if value == 0:
        return None
    if 1 <= value <= 100:
        return f"{value % 100:02d}"
    if 101 <= value <= 110:
        return str(value % 10)
    return None


def pkt_direct_dial(ext: str | None) -> bytes:
    return pkt(DIRECT_DIAL, bytes([encode_extension(ext)]))


_NAMES = {HEARTBEAT: "Heartbeat", DIRECT_DIAL: "Durchwahl", BAUDOT: "Baudot", END: "Ende",
          REJECT: "Abweisung", ACK: "Ack", VERSION: "Version", SELFTEST: "Selbsttest",
          REMOTE_CONFIG: "Fernkonfig"}


def describe(ptype: int, payload: bytes) -> str:
    """Lesbare Kurzform eines Pakets für das Debug-Protokoll."""
    name = _NAMES.get(ptype, f"Typ {ptype:#04x}")
    if ptype == BAUDOT:
        text = BaudotDecoder().decode(payload)  # Umschaltzustand kann abweichen, nur zur Anzeige
        return f"{name} {payload.hex()} {text!r}"
    if ptype == REJECT:
        return f"{name} {payload.decode('ascii', 'ignore').rstrip(chr(0))!r}"
    if ptype in (ACK, DIRECT_DIAL) and payload:
        return f"{name} {payload[0]}"
    if ptype == VERSION and payload:
        return f"{name} {payload[0]} {payload[1:].decode('ascii', 'ignore').rstrip(chr(0))!r}"
    return f"{name} {payload.hex()}" if payload else name


KNOWN_TYPES = {HEARTBEAT, DIRECT_DIAL, BAUDOT, END, REJECT, ACK, VERSION,
               SELFTEST, REMOTE_CONFIG}


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readexactly(2)
    payload = await reader.readexactly(head[1]) if head[1] else b""
    return head[0], payload
