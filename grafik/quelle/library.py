"""Eigene Fernschreib-Grafiken. Nur ITA2-Zeichen, höchstens 68 Zeichen breit."""

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
