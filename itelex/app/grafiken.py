"""Mitgelieferte Fernschreib-Grafiken: nur Zeichen, die ein Fernschreiber drucken kann (ITA2),
höchstens 68 Zeichen breit. Alle Motive sind für dieses Projekt gezeichnet."""

BILDER = [
    ("fernschreiber", "Fernschreiber", "Bilder", r"""
        .--------------------.
        I ZCZC               I
        I GRUSS AUS FFM ..   I
        I                    I
    .---'--------------------'---.
    I  ( ) ( ) ( ) ( ) ( ) ( )   I
    I   ( ) ( ) ( ) ( ) ( ) ( )  I
    I     (================)     I
    '----------------------------'
"""),
    ("herz", "Herz", "Bilder", r"""
    .XXXXX.   .XXXXX.
   XXXXXXXXX.XXXXXXXXX
   XXXXXXXXXXXXXXXXXXX
   'XXXXXXXXXXXXXXXXX'
     'XXXXXXXXXXXXX'
       'XXXXXXXXX'
         'XXXXX'
           'X'
"""),
    ("tannenbaum", "Tannenbaum", "Bilder", r"""
              +
             .X.
            .XXX.
           .XXOXX.
            .XXX.
          .XXXXXXX.
         .XOXXXXXOX.
           .XXXXX.
        .XXXXXOXXXXX.
       .XOXXXXXXXXXOX.
      .XXXXXXXXXXXXXXX.
             III
             III
         -----------
"""),
    ("haus", "Haus", "Bilder", r"""
               .:.
             .'   '.
           .'       '.
         .'           '.
       .'               '.
      '-------------------'
        I .---.   .---. I
        I I-+-I   I   I I
        I '---'   I  .I I
        I         I   I I
      =====================
"""),
    ("kaffee", "Kaffee", "Bilder", r"""
         (  (  (
          )  )  )
        .---------.
        I         I--.
        I  KAFFEE I  I
        I         I--'
        '---------'
      '-------------'
"""),
    ("torte", "Geburtstag", "Bilder", r"""
          .    .    .
          I    I    I
      .---I----I----I---.
      I                 I
      I   ALLES  GUTE   I
      I=================I
      I                 I
      '-----------------'
"""),
    ("brief", "Brief", "Bilder", r"""
    .-----------------------.
    I'.                   .'I
    I  '.               .'  I
    I    '.           .'    I
    I      '.       .'      I
    I        '-----'        I
    I                       I
    '-----------------------'
"""),
    ("segelboot", "Segelboot", "Bilder", r"""
             I
            /I
           / I
          /  I
         /   I
        /    I
       /-----I
             I
    .--------I--------.
     '.             .'
       '-----------'
   =-=-=-=-=-=-=-=-=-=-=-=
"""),
    ("sonne", "Sonne", "Bilder", r"""
          .    I    .
            .  I  .
         .  .-----.  .
           (       )
     ---- (         ) ----
           (       )
         '  '-----'  '
            '  I  '
          '    I    '
"""),
]

LINIEN = [
    ("zczc", "Telex-Anfang", "Linien", "ZCZC"),
    ("nnnn", "Telex-Ende", "Linien", "NNNN"),
    ("linie_gleich", "Doppellinie", "Linien", "=" * 40),
    ("linie_wechsel", "Wechsellinie", "Linien", "=-" * 20),
    ("linie_kreuz", "Kreuzlinie", "Linien", "+---" * 10 + "+"),
    ("linie_punkte", "Punkte", "Linien", ". : " * 10),
    ("linie_wellen", "Wellen", "Linien", "()" * 20),
]


def clean(text: str) -> str:
    lines = text.strip("\n").split("\n")
    lines = [l.rstrip() for l in lines]
    indent = min((len(l) - len(l.lstrip(" ")) for l in lines if l.strip()), default=0)
    return "\n".join(l[indent:] for l in lines)


def all_items():
    return [{"id": i, "name": n, "kategorie": k, "text": clean(t)} for i, n, k, t in BILDER + LINIEN]


ITA2 = set(" ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-':()+=/?,.")
MAX_WIDTH, MAX_LINES, MAX_CHARS = 68, 400, 16000


def validate(text: str) -> str:
    """Grafik prüfen und vereinheitlichen; ValueError mit verständlicher Meldung."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ").upper()
    lines = [l.rstrip() for l in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ValueError("Die Grafik ist leer")
    bad = sorted({c for l in lines for c in l if c not in ITA2})
    if bad:
        raise ValueError("Diese Zeichen kann ein Fernschreiber nicht drucken: " + " ".join(bad))
    if max(len(l) for l in lines) > MAX_WIDTH:
        raise ValueError(f"Höchstens {MAX_WIDTH} Zeichen pro Zeile")
    if len(lines) > MAX_LINES:
        raise ValueError(f"Höchstens {MAX_LINES} Zeilen")
    out = "\n".join(lines)
    if len(out) > MAX_CHARS:
        raise ValueError(f"Höchstens {MAX_CHARS} Zeichen")
    return out


_BUILTIN = {i["id"]: i for i in all_items()}


def builtin() -> list[dict]:
    return list(_BUILTIN.values())


def lookup(key: str, store=None) -> str | None:
    """Grafik über ihre Kennung (z. B. "herz") oder den Namen einer eigenen Grafik finden."""
    key = key.strip().lower()
    if key in _BUILTIN:
        return _BUILTIN[key]["text"]
    for item in _BUILTIN.values():
        if item["name"].lower() == key:
            return item["text"]
    if store is not None:
        for g in store.grafiken():
            if g["name"].lower() == key or str(g["id"]) == key:
                return g["text"]
    return None
