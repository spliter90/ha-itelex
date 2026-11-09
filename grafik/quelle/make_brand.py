"""Banner (1280x320) und GitHub-Vorschaubild (1280x640) für ha-itelex."""
import asyncio, sys, html
sys.path.insert(0, "/home/claude/ha-itelex/itelex/app")
import protocol as P
from playwright.async_api import async_playwright
OUT = sys.argv[1]
ICON = open("/home/claude/ha-itelex/grafik/itelex_icon.svg").read()
VX = open("/home/claude/ha-itelex/grafik/vx_icon.svg").read()

def tape(text, pitch=17, hole=9.5, h=86, color="#e9dcb8", ink="#2a2621"):
    """Echter 5-Spur-Lochstreifen (ITA2): Spur 1–2, Transportloch, Spur 3–5."""
    codes = P.BaudotEncoder()._encode(P.normalize(text))
    codes = bytes([0x1F] * 3) + codes + bytes([0x1F] * 3)      # Vor-/Nachlauf mit Bu-Zeichen
    w = len(codes) * pitch + pitch
    # 6 gleich verteilte Reihen: Spur 1, Spur 2, Transportloch, Spur 3, Spur 4, Spur 5
    rows = [h * (0.15 + k * 0.14) + 4 for k in range(6)]
    track_y = [rows[0], rows[1], rows[3], rows[4], rows[5]]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h + 8}" viewBox="0 0 {w} {h + 8}" style="display:block;flex-shrink:0">',
           f'<rect x="0" y="0" width="{w}" height="{h + 8}" fill="{color}"/>',
           f'<rect x="0" y="0" width="{w}" height="2" fill="#d6c79d"/><rect x="0" y="{h + 6}" width="{w}" height="2" fill="#d6c79d"/>']
    for i, c in enumerate(codes):
        x = pitch * (i + 1)
        out.append(f'<circle cx="{x}" cy="{rows[2]:.1f}" r="{hole * 0.28:.1f}" fill="{ink}" opacity=".85"/>')
        for bit in range(5):
            if c >> bit & 1:
                out.append(f'<circle cx="{x}" cy="{track_y[bit]:.1f}" r="{hole / 2:.1f}" fill="{ink}"/>')
    out.append("</svg>")
    return "".join(out), w

def page(w, h, body):
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    * {{ box-sizing: border-box; margin: 0; }}
    body {{ width: {w}px; height: {h}px; overflow: hidden; background: #f3eee4; font-family: Inter, sans-serif; color: #24211c; position: relative; }}
    .mono {{ font-family: "DejaVu Sans Mono", monospace; }}
    .type {{ font-family: "Courier 10 Pitch", "Courier", monospace; }}
    .grain {{ position: absolute; inset: 0; background:
        repeating-linear-gradient(0deg, rgba(36,33,28,.035) 0 1px, transparent 1px 34px); }}
    .sheet {{ position: absolute; background: #fffdf7; box-shadow: 0 18px 40px rgba(36,33,28,.16), 0 2px 6px rgba(36,33,28,.12);
        border-radius: 3px; padding: 26px 30px 30px; }}
    .sheet .row {{ font-size: 21px; line-height: 1.55; letter-spacing: .5px; white-space: pre; color: #2b2723; }}
    .sheet .ab {{ color: #8c857a; }}
    .sheet .hl {{ color: #b4441f; }}
    .sheet::before {{ content: ""; position: absolute; left: 0; right: 0; top: -1px; height: 12px;
        background: radial-gradient(circle at 10px 6px, #f3eee4 4px, transparent 4.5px) 0 0 / 20px 12px repeat-x; }}
    .chip {{ display: inline-block; font-size: 17px; padding: 6px 13px; border-radius: 999px; background: #e9e2d4; color: #4a453d; margin-right: 8px; }}
    .cursor {{ display: inline-block; width: 12px; height: 22px; background: #b4441f; vertical-align: -4px; }}
    </style></head><body><div class="grain"></div>{body}</body></html>"""

SHEET = """<div class="row ab">11111 ALPHA D</div>
<div class="row ab">33333 CHRIS D</div>
<div class="row"> </div>
<div class="row">09.11.26 11:20</div>
<div class="row">GRUSS AUS FRANKFURT -</div>
<div class="row">GESENDET AUS <span class="hl">HOME ASSISTANT</span>.</div>
<div class="row">MFG CHRIS<span class="cursor"></span></div>"""

def banner():
    t, tw = tape("ZCZC I-TELEX FUER HOME ASSISTANT NNNN", pitch=15, hole=8.5, h=62)
    body = f"""
    <div style="position:absolute;left:0;right:0;bottom:16px;height:84px;overflow:hidden">
      <div style="position:absolute;left:-40px;top:0;transform:rotate(-1.2deg);transform-origin:left;display:flex">{t}{t}</div></div>
    <div class="sheet type" style="right:56px;top:-14px;width:430px;transform:rotate(1.6deg)">{SHEET}</div>
    <div style="position:absolute;left:58px;top:46px;width:128px;height:128px">{ICON}</div>
    <div style="position:absolute;left:214px;top:44px">
      <div class="mono" style="font-weight:700;font-size:78px;letter-spacing:6px;line-height:1">i-TELEX</div>
      <div style="font-size:25px;margin-top:14px;color:#4a453d;font-weight:500">Fernschreiben mit Home Assistant</div>
      <div style="margin-top:16px"><span class="chip">Station</span><span class="chip">Vermittlung</span><span class="chip">Assist</span></div>
    </div>"""
    return page(1280, 320, body)

def social():
    t, tw = tape("ZCZC I-TELEX FUER HOME ASSISTANT - SENDEN EMPFANGEN VERMITTELN NNNN", pitch=18, hole=10, h=80)
    body = f"""
    <div style="position:absolute;left:0;right:0;bottom:46px;height:92px;overflow:hidden">
      <div style="position:absolute;left:-30px;top:4px;transform:rotate(-1.4deg);transform-origin:left;display:flex">{t}{t}</div></div>
    <div class="sheet type" style="right:70px;top:-16px;width:480px;transform:rotate(1.8deg);padding-top:34px">{SHEET}</div>
    <div style="position:absolute;left:84px;top:96px;display:flex;gap:18px">
      <div style="width:132px;height:132px">{ICON}</div><div style="width:132px;height:132px">{VX}</div></div>
    <div style="position:absolute;left:84px;top:262px;width:640px">
      <div class="mono" style="font-weight:700;font-size:92px;letter-spacing:7px;line-height:1">i-TELEX</div>
      <div style="font-size:31px;margin-top:18px;color:#3a3631;font-weight:500;line-height:1.3">Telex senden und empfangen,<br>direkt in Home Assistant</div>
      <div class="mono" style="font-size:18px;margin-top:22px;color:#8c857a">github.com/spliter90/ha-itelex</div>
    </div>"""
    return page(1280, 640, body)

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome")
        for name, (w, h, doc) in {"banner": (1280, 320, banner()), "social-preview": (1280, 640, social())}.items():
            open(f"{OUT}/{name}.html", "w").write(doc)
            pg = await b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2 if name == "banner" else 1)
            await pg.set_content(doc); await pg.wait_for_timeout(300)
            await pg.screenshot(path=f"{OUT}/{name}.png")
        await b.close()
asyncio.run(main())
