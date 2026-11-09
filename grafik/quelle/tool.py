"""Hilfen: Fernschreibzeichensatz, Glyphendaten messen, Text wie auf Papier rendern."""
import json, sys
from PIL import Image, ImageDraw, ImageFont
ITA2 = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-':()+=/?,."
FONT = "/usr/share/fonts/X11/Type1/c0419bt_.pfb"
CELL_W, ASPECT = 30, 1.667          # 2,54 mm Zeichen, 4,23 mm Zeile
CELL_H = round(CELL_W * ASPECT)

def font(size=None):
    import subprocess
    path = subprocess.run(["fc-match", "-f", "%{file}", "Courier 10 Pitch"], capture_output=True, text=True).stdout
    return ImageFont.truetype(path, size or int(CELL_W * 1.66))

def render(text, out, scale=1.0, paper=(252, 249, 240), ink=(40, 36, 31)):
    lines = text.split("\n")
    w = max(len(l) for l in lines) if lines else 1
    f = font()
    img = Image.new("RGB", (int((w + 4) * CELL_W), int((len(lines) + 2) * CELL_H)), paper)
    d = ImageDraw.Draw(img)
    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch != " ":
                d.text(((c + 2) * CELL_W + CELL_W / 2, (r + 1) * CELL_H + CELL_H / 2), ch, font=f, fill=ink, anchor="mm")
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img.save(out)

def glyph_data(gx=3, gy=5):
    """Tintenanteil je Teilfeld (gx x gy) für jedes druckbare Zeichen."""
    f = font()
    data = {}
    for ch in ITA2:
        img = Image.new("L", (CELL_W * 4, CELL_H * 4), 0)
        d = ImageDraw.Draw(img)
        f4 = font(int(CELL_W * 1.66 * 4))
        d.text((CELL_W * 2, CELL_H * 2), ch, font=f4, fill=255, anchor="mm")
        px = img.load()
        vals = []
        for j in range(gy):
            for i in range(gx):
                x0, x1 = i * img.width // gx, (i + 1) * img.width // gx
                y0, y1 = j * img.height // gy, (j + 1) * img.height // gy
                s = sum(px[x, y] for x in range(x0, x1) for y in range(y0, y1))
                vals.append(round(s / (255 * (x1 - x0) * (y1 - y0)), 4))
        data[ch] = vals
    return data

def check(text, width=68):
    bad = sorted({c for c in text if c not in ITA2 + "\n"})
    wide = [i for i, l in enumerate(text.split("\n")) if len(l) > width]
    return bad, wide

if __name__ == "__main__":
    d = glyph_data()
    dens = sorted(((sum(v) / len(v), ch) for ch, v in d.items()))
    print("Dichte hell -> dunkel:", "".join(ch for _, ch in dens))
    print("max:", round(dens[-1][0], 3), dens[-1][1])
    json.dump({"gx": 3, "gy": 5, "glyphs": d}, open(sys.argv[1], "w"), separators=(",", ":"))
