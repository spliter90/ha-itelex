/* Telex-Grafiken: Auswahl, Schriftzug-Generator und Foto-Umwandler.
   Alles besteht nur aus Zeichen, die ein Fernschreiber drucken kann (ITA2). */
(function () {
  "use strict";
  const ITA2 = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-':()+=/?,.";
  const ITA2SET = new Set(ITA2);
  const WIDTH = 68, ASPECT = 1.667, CPS = 6.67;          // 50 Baud ≈ 6,67 Zeichen/s
  const G = __GLYPHS__;                                  // gemessene Tintenanteile je Zeichen (3x5 Felder)

  // ---------- Hilfen ----------
  const $ = (s, el = document) => el.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const REPL = { "Ä": "AE", "Ö": "OE", "Ü": "UE", "ß": "SS", "!": ".", '"': "'", ";": ",", "_": "-", "&": "+" };
  function norm(t) {
    return [...String(t).toUpperCase()].map(c => REPL[c] ?? (ITA2SET.has(c) || c === "\n" ? c : "")).join("");
  }
  function tidy(t) {
    const lines = t.split("\n").map(l => l.replace(/\s+$/, ""));
    while (lines.length && !lines[0].trim()) lines.shift();
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    return lines.join("\n");
  }
  function stats(t) {
    const lines = t ? t.split("\n") : [];
    const w = Math.max(0, ...lines.map(l => l.length));
    const chars = lines.reduce((a, l) => a + l.length + 2, 0);
    const s = Math.round(chars / CPS);
    const time = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
    return { w, h: lines.length, chars, time, text: `${w} × ${lines.length} · ${chars} Zeichen · Druckzeit ca. ${time}` };
  }

  // ---------- Schriftzug (5x7-Zeichensatz) ----------
  const F = {
    A: ".###.|#...#|#...#|#####|#...#|#...#|#...#", B: "####.|#...#|#...#|####.|#...#|#...#|####.",
    C: ".###.|#...#|#....|#....|#....|#...#|.###.", D: "####.|#...#|#...#|#...#|#...#|#...#|####.",
    E: "#####|#....|#....|####.|#....|#....|#####", F: "#####|#....|#....|####.|#....|#....|#....",
    G: ".###.|#...#|#....|#.###|#...#|#...#|.####", H: "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    I: ".###.|..#..|..#..|..#..|..#..|..#..|.###.", J: "..###|...#.|...#.|...#.|...#.|#..#.|.##..",
    K: "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#", L: "#....|#....|#....|#....|#....|#....|#####",
    M: "#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#", N: "#...#|#...#|##..#|#.#.#|#..##|#...#|#...#",
    O: ".###.|#...#|#...#|#...#|#...#|#...#|.###.", P: "####.|#...#|#...#|####.|#....|#....|#....",
    Q: ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#", R: "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    S: ".####|#....|#....|.###.|....#|....#|####.", T: "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    U: "#...#|#...#|#...#|#...#|#...#|#...#|.###.", V: "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    W: "#...#|#...#|#...#|#.#.#|#.#.#|#.#.#|.#.#.", X: "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    Y: "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..", Z: "#####|....#|...#.|..#..|.#...|#....|#####",
    0: ".###.|#...#|#..##|#.#.#|##..#|#...#|.###.", 1: "..#..|.##..|..#..|..#..|..#..|..#..|.###.",
    2: ".###.|#...#|....#|...#.|..#..|.#...|#####", 3: "#####|...#.|..#..|...#.|....#|#...#|.###.",
    4: "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.", 5: "#####|#....|####.|....#|....#|#...#|.###.",
    6: "..##.|.#...|#....|####.|#...#|#...#|.###.", 7: "#####|....#|...#.|..#..|.#...|.#...|.#...",
    8: ".###.|#...#|#...#|.###.|#...#|#...#|.###.", 9: ".###.|#...#|#...#|.####|....#|...#.|.##..",
    "-": ".....|.....|.....|#####|.....|.....|.....", ".": ".....|.....|.....|.....|.....|.##..|.##..",
    "?": ".###.|#...#|....#|...#.|..#..|.....|..#..", ":": ".....|.##..|.##..|.....|.##..|.##..|.....",
    ",": ".....|.....|.....|.....|.##..|..#..|.#...", "'": "..#..|..#..|.#...|.....|.....|.....|.....",
    "(": "...#.|..#..|.#...|.#...|.#...|..#..|...#.", ")": ".#...|..#..|...#.|...#.|...#.|..#..|.#...",
    "+": ".....|..#..|..#..|#####|..#..|..#..|.....", "=": ".....|.....|#####|.....|#####|.....|.....",
    "/": "....#|....#|...#.|..#..|.#...|#....|#....", " ": ".....|.....|.....|.....|.....|.....|.....",
  };
  function wrapWords(text, max) {
    const out = [];
    for (const para of text.split("\n")) {
      let line = "";
      for (let word of para.split(/\s+/).filter(Boolean)) {
        while (word.length > max) { if (line) { out.push(line); line = ""; } out.push(word.slice(0, max)); word = word.slice(max); }
        if (!line) line = word; else if (line.length + 1 + word.length <= max) line += " " + word; else { out.push(line); line = word; }
      }
      out.push(line);
    }
    return out.filter((l, i, a) => l || (i > 0 && i < a.length - 1));
  }
  function blockText(text, { fill = "letter", wide = true } = {}) {
    text = norm(text).trim();
    if (!text) return "";
    const cw = wide ? 12 : 6, maxLetters = Math.floor((WIDTH + (wide ? 2 : 1)) / cw);
    const blocks = [];
    for (const line of wrapWords(text, maxLetters)) {
      const rows = Array.from({ length: 7 }, () => "");
      [...line].forEach((ch, idx) => {
        const pat = (F[ch] || F[" "]).split("|");
        const fc = fill === "letter" ? (/[A-Z0-9]/.test(ch) ? ch : "X") : fill;
        for (let r = 0; r < 7; r++) {
          let seg = [...pat[r]].map(p => (p === "#" ? fc : " ").repeat(wide ? 2 : 1)).join("");
          rows[r] += (idx ? " ".repeat(wide ? 2 : 1) : "") + seg;
        }
      });
      blocks.push(rows.map(r => r.replace(/\s+$/, "")).join("\n"));
    }
    return blocks.join("\n\n");
  }
  // Banner: Text läuft längs über das Papier (zum Lesen nach links drehen), beliebig lang.
  // Buchstabenoberkante liegt rechts, Buchstabenanfang oben.
  const BANNER = { klein: [2, 1], mittel: [4, 2], gross: [8, 5] };     // [Spalten, Zeilen] je Bildpunkt
  function bannerText(text, { fill = "letter", size = "mittel" } = {}) {
    text = norm(text).replace(/\n/g, " ").replace(/\s+/g, " ").trim();
    if (!text) return "";
    const [sx, sy] = BANNER[size] || BANNER.mittel;
    const margin = 0;   // keine Einrückung: jedes Leerzeichen kostet Druckzeit
    const out = [];
    [...text].forEach((ch, idx) => {
      const pat = (F[ch] || F[" "]).split("|");
      const fc = fill === "letter" ? (/[A-Z0-9]/.test(ch) ? ch : "X") : fill;
      if (idx) for (let k = 0; k < sy; k++) out.push("");              // Abstand zwischen Buchstaben
      for (let x = 0; x < 5; x++) {
        let row = "";
        for (let c = 0; c < 7; c++) row += (pat[6 - c][x] === "#" ? fc : " ").repeat(sx);
        row = (" ".repeat(margin) + row).replace(/\s+$/, "");
        for (let k = 0; k < sy; k++) out.push(row);
      }
    });
    return tidy(out.join("\n"));
  }

  function frameText(text, style = "einfach") {
    text = norm(text).trim();
    if (!text) return "";
    const lines = wrapWords(text, WIDTH - 6);
    const inner = Math.max(...lines.map(l => l.length)) + 4;
    const S = { einfach: [".", "-", ".", "I", "'", "-", "'"], doppelt: ["+", "=", "+", "I", "+", "=", "+"],
                kraeftig: ["X", "X", "X", "X", "X", "X", "X"], wellen: ["(", "-", ")", "I", "(", "-", ")"] }[style];
    const mid = l => { const pad = inner - l.length, a = Math.floor(pad / 2); return S[3] + " ".repeat(a) + l + " ".repeat(pad - a) + S[3]; };
    return [S[0] + S[1].repeat(inner) + S[2], mid(""), ...lines.map(mid), mid(""), S[4] + S[5].repeat(inner) + S[6]].join("\n");
  }

  // ---------- Foto -> Zeichenbild ----------
  // Getestete Zeichenvorräte: wenige, klar gestufte Zeichen wirken ruhiger als alle 48
  const ALL = Object.entries(G.glyphs).map(([ch, v]) => ({ ch, v, m: v.reduce((a, b) => a + b, 0) / v.length }));
  const MAXM = Math.max(...ALL.map(g => g.m));
  const GLY = ALL.filter(g => " .',:-=+/()IXOM8".includes(g.ch));          // Konturen
  const RAMP = ALL.filter(g => " .':-=+(IXOM".includes(g.ch)).sort((a, b) => a.m - b.m);   // Flächen
  function convertImage(img, { cols = 40, contrast = 1, brightness = 0, invert = false, mode = "kraeftig" } = {}) {
    cols = Math.max(10, Math.min(WIDTH, Math.round(cols)));
    let rows = Math.max(1, Math.round(cols * img.height / img.width / ASPECT));
    if (rows > 60) { cols = Math.max(10, Math.round(cols * 60 / rows)); rows = 60; }
    const gx = G.gx, gy = G.gy, w = cols * gx, h = rows * gy;
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h);
    ctx.imageSmoothingQuality = "high"; ctx.drawImage(img, 0, 0, w, h);
    const px = ctx.getImageData(0, 0, w, h).data;
    const d = new Float32Array(w * h);
    for (let i = 0; i < w * h; i++) d[i] = 1 - (0.2126 * px[i * 4] + 0.7152 * px[i * 4 + 1] + 0.0722 * px[i * 4 + 2]) / 255;
    // automatische Tonwertspreizung (2 % / 98 %), dann Kontrast und Helligkeit
    const sorted = Float32Array.from(d).sort();
    const lo = sorted[Math.floor(sorted.length * 0.02)], hi = sorted[Math.floor(sorted.length * 0.98)] || 1;
    const span = Math.max(0.05, hi - lo);
    for (let i = 0; i < d.length; i++) {
      let v = (d[i] - lo) / span;
      v = (v - 0.5) * contrast + 0.5 - brightness;
      if (invert) v = 1 - v;
      d[i] = Math.max(0, Math.min(1, v));
    }
    const out = [];
    if (mode === "fein") {
      for (let r = 0; r < rows; r++) {
        let line = "";
        for (let cI = 0; cI < cols; cI++) {
          const t = [];
          for (let j = 0; j < gy; j++) for (let i = 0; i < gx; i++) t.push(d[(r * gy + j) * w + cI * gx + i] * MAXM);
          const tm = t.reduce((a, b) => a + b, 0) / t.length;
          let best = " ", be = Infinity;
          for (const g of GLY) {
            let e = 3 * t.length * (g.m - tm) ** 2;                 // Helligkeit zählt mit
            for (let k = 0; k < t.length; k++) { const x = g.v[k] - t[k]; e += x * x; }
            if (e < be) { be = e; best = g.ch; }
          }
          line += best;
        }
        out.push(line);
      }
    } else {
      // kräftig: nur Helligkeit je Zeichen, mit Fehlerverteilung (Floyd-Steinberg)
      const m = Array.from({ length: rows }, (_, r) => Array.from({ length: cols }, (_, cI) => {
        let s = 0; for (let j = 0; j < gy; j++) for (let i = 0; i < gx; i++) s += d[(r * gy + j) * w + cI * gx + i];
        return s / (gx * gy) * MAXM;
      }));
      for (let r = 0; r < rows; r++) {
        let line = "";
        for (let cI = 0; cI < cols; cI++) {
          const v = m[r][cI];
          let best = RAMP[0], be = Infinity;
          for (const g of RAMP) { const e = Math.abs(g.m - v); if (e < be) { be = e; best = g; } }
          const err = v - best.m;
          if (cI + 1 < cols) m[r][cI + 1] += err * 7 / 16;
          if (r + 1 < rows) { if (cI) m[r + 1][cI - 1] += err * 3 / 16; m[r + 1][cI] += err * 5 / 16; if (cI + 1 < cols) m[r + 1][cI + 1] += err / 16; }
          line += best.ch;
        }
        out.push(line);
      }
    }
    return tidy(out.join("\n"));
  }

  // ---------- Oberfläche ----------
  const CSS = `
  .gx-ov { position: fixed; inset: 0; background: rgba(0,0,0,.38); z-index: 50; display: flex; align-items: flex-start; justify-content: center; padding: 18px 10px; overflow-y: auto; }
  .gx-dlg { background: var(--paper); color: var(--ink); width: 100%; max-width: 760px; border-radius: 12px; border: 1px solid var(--line); box-shadow: 0 12px 34px rgba(0,0,0,.28); display: flex; flex-direction: column; max-height: calc(100vh - 36px); }
  .gx-head, .gx-tabs, .gx-foot { flex-shrink: 0; }
  .gx-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--line); }
  .gx-head b { flex: 1; font-size: 16px; }
  .gx-tabs { display: flex; gap: 2px; padding: 0 10px; border-bottom: 1px solid var(--line); overflow-x: auto; scrollbar-width: none; }
  .gx-tabs button { flex-shrink: 0; border: 0; background: none; padding: 9px 10px; color: var(--ink-2); border-bottom: 2px solid transparent; font-size: 14px; }
  .gx-tabs button.on { color: var(--ink); border-color: var(--accent); font-weight: 600; }
  .gx-body { padding: 14px 16px; overflow-y: auto; flex: 1; min-height: 200px; }
  .gx-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
  .gx-card { position: relative; border: 1px solid var(--line); background: #fffdf7; color: #2b2723; border-radius: 8px; padding: 10px 8px 8px; text-align: left; cursor: pointer; display: flex; flex-direction: column; gap: 6px; min-height: 120px; }
  .gx-card.on { outline: 2px solid var(--accent); outline-offset: -1px; }
  .gx-card .nm { font: 600 12.5px var(--sans); color: #4a453d; }
  .gx-card pre { margin: 0; font-family: "Courier New", Courier, var(--mono); line-height: 1; overflow: hidden; flex: 1; display: flex; align-items: center; justify-content: center; white-space: pre; }
  .gx-card .del { position: absolute; top: 4px; right: 4px; border: 0; background: none; color: var(--err); font-size: 15px; padding: 2px 6px; }
  .gx-paper { background: #fffdf7; color: #2b2723; border: 1px solid var(--line); border-radius: 8px; padding: 12px; overflow: auto; max-height: 46vh; }
  .gx-paper pre { margin: 0; font-family: "Courier New", Courier, var(--mono); font-size: 12px; line-height: 1; white-space: pre; }
  .gx-form { display: grid; gap: 10px; margin-bottom: 12px; }
  .gx-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .gx-row label { font-size: 13px; color: var(--ink-2); display: flex; align-items: center; gap: 6px; }
  .gx-form input[type=text], .gx-form select, .gx-form textarea { padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper-2); font-size: 14px; }
  .gx-form input[type=text] { flex: 1; min-width: 160px; font-family: var(--mono); text-transform: uppercase; }
  .gx-form textarea { width: 100%; min-height: 120px; font: 12px/1.15 var(--mono); white-space: pre; overflow-x: auto; }
  .gx-drop { border: 2px dashed var(--line); border-radius: 10px; padding: 18px; text-align: center; color: var(--ink-2); cursor: pointer; }
  .gx-drop.over { border-color: var(--accent); color: var(--accent); }
  .gx-foot { display: flex; align-items: center; gap: 8px; padding: 10px 16px; border-top: 1px solid var(--line); flex-wrap: wrap; }
  .gx-foot .info { flex: 1; font-size: 12.5px; color: var(--ink-2); min-width: 160px; }
  .gx-hint { font-size: 12.5px; color: var(--ink-2); margin: 10px 2px 0; }
  .gx-warn { color: var(--warn); }
  `;
  let st = null;
  function fitFont(t) {
    const s = stats(t), byW = 124 / Math.max(4, s.w) / 0.6, byH = 92 / Math.max(1, s.h);
    return Math.max(2.4, Math.min(11, byW, byH)).toFixed(2);
  }
  async function api(path, opts = {}) {
    const r = await fetch("api/" + path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
    return data;
  }
  function note(t) { (window.toast || alert)(t); }

  async function open(onInsert) {
    if (!$("#gx-css")) { const s = document.createElement("style"); s.id = "gx-css"; s.textContent = CSS; document.head.appendChild(s); }
    const ov = document.createElement("div");
    ov.className = "gx-ov";
    ov.innerHTML = `<div class="gx-dlg" role="dialog" aria-label="Grafik einfügen">
      <div class="gx-head"><b>Grafik einfügen</b><button class="iconbtn" data-x>Schließen</button></div>
      <div class="gx-tabs">${["Bilder", "Linien", "Schriftzug", "Foto", "Eigene"].map(t => `<button data-tab="${t}">${t}</button>`).join("")}</div>
      <div class="gx-body"></div>
      <div class="gx-foot"><span class="info"></span><button class="iconbtn" data-save hidden>Als eigene speichern</button><button class="primary" data-ins disabled>Einfügen</button></div></div>`;
    document.body.appendChild(ov);
    st = { ov, onInsert, text: "", tab: "Bilder", lib: { mitgeliefert: [], eigene: [] } };
    const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = e => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    ov.addEventListener("click", e => { if (e.target === ov) close(); });
    $("[data-x]", ov).onclick = close;
    $("[data-ins]", ov).onclick = () => { if (st.text) { onInsert(st.text); close(); } };
    $("[data-save]", ov).onclick = async () => {
      const name = prompt("Name für die eigene Grafik:");
      if (!name) return;
      try { await api("grafiken", { method: "POST", body: { name, text: st.text } }); note("Gespeichert unter „Eigene“"); await loadLib(); }
      catch (e) { note(e.message); }
    };
    ov.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => show(b.dataset.tab));
    await loadLib();
    show("Bilder");
  }
  async function loadLib() { try { st.lib = await api("grafiken"); } catch (e) { note(e.message); } }
  function setText(t, canSave) {
    st.text = t || "";
    const s = stats(st.text);
    $(".info", st.ov).innerHTML = st.text ? esc(s.text) + (s.chars > 5000 ? ` <span class="gx-warn">· sehr lang, belegt die Gegenstelle lange</span>` : s.chars > 1400 ? ` <span class="gx-warn">· lang für 50 Baud</span>` : "") : "Nichts ausgewählt";
    $("[data-ins]", st.ov).disabled = !st.text;
    $("[data-save]", st.ov).hidden = !(canSave && st.text);
  }
  function show(tab) {
    st.tab = tab;
    st.ov.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
    const body = $(".gx-body", st.ov);
    setText("", false);
    if (tab === "Bilder" || tab === "Linien" || tab === "Eigene") {
      const items = tab === "Eigene" ? st.lib.eigene.map(g => ({ ...g, own: true })) : st.lib.mitgeliefert.filter(g => g.kategorie === tab);
      body.innerHTML = items.length ? `<div class="gx-grid">${items.map((g, i) => `
        <div class="gx-card" data-i="${i}" tabindex="0">${g.own ? `<button class="del" data-del="${g.id}" title="Löschen">✕</button>` : ""}
          <span class="nm">${esc(g.name)}${tab !== "Eigene" ? ` <span style="font-weight:400;color:#8c857a">· {grafik:${esc(g.id)}}</span>` : ""}</span>
          <pre style="font-size:${fitFont(g.text)}px">${esc(g.text)}</pre></div>`).join("")}</div>
        <p class="gx-hint">Tipp: In Automationen oder per Assist kannst du eine Grafik mit <b>{grafik:name}</b> im Text mitschicken.</p>`
        : `<p class="gx-hint">Noch keine eigenen Grafiken. Erstelle eine unter „Schriftzug“ oder „Foto“ und tippe auf „Als eigene speichern“.</p>`;
      body.querySelectorAll(".gx-card").forEach(card => {
        const pick = () => { body.querySelectorAll(".gx-card").forEach(c => c.classList.remove("on")); card.classList.add("on"); setText(items[card.dataset.i].text, false); };
        card.onclick = e => { if (!e.target.closest("[data-del]")) pick(); };
        card.ondblclick = () => { pick(); $("[data-ins]", st.ov).click(); };
        card.onkeydown = e => { if (e.key === "Enter") pick(); };
      });
      body.querySelectorAll("[data-del]").forEach(b => b.onclick = async e => {
        e.stopPropagation();
        if (!confirm("Diese eigene Grafik löschen?")) return;
        await api("grafiken/" + b.dataset.del, { method: "DELETE" }); await loadLib(); show("Eigene");
      });
    } else if (tab === "Schriftzug") {
      body.innerHTML = `<div class="gx-form">
        <div class="gx-row"><input type="text" id="gx-word" maxlength="80" placeholder="Text, z. B. FROHE WEIHNACHTEN" value="HALLO"></div>
        <div class="gx-row">
          <label>Art <select id="gx-style"><option value="block-breit">Blockschrift breit</option><option value="block">Blockschrift schmal</option><option value="banner-mittel">Banner längs (mittel)</option><option value="banner-klein">Banner längs (klein)</option><option value="banner-gross">Banner längs (groß)</option><option value="rahmen-einfach">Rahmen einfach</option><option value="rahmen-doppelt">Rahmen doppelt</option><option value="rahmen-kraeftig">Rahmen kräftig</option><option value="rahmen-wellen">Rahmen rund</option></select></label>
          <label id="gx-filllab">Zeichen <select id="gx-fill"><option value="letter">Buchstabe selbst</option><option value="X">X</option><option value="M">M (dunkel)</option><option value="O">O</option><option value="8">8</option><option value="+">+ (hell)</option><option value=":">: (zart)</option></select></label>
        </div></div>
        <div class="gx-paper"><pre id="gx-out"></pre></div>
        <p class="gx-hint" id="gx-bhint" hidden>Banner laufen längs über das Papier und können beliebig lang sein. Zum Lesen den Ausdruck nach links drehen. Achte auf die Druckzeit: Groß braucht pro Buchstabe rund eine Minute.</p>
        <p class="gx-hint">Blockschrift breit: bis 5 Buchstaben pro Zeile, schmal: bis 11. Längere Texte werden auf mehrere Zeilen verteilt.</p>`;
      const run = () => {
        const style = $("#gx-style").value, word = $("#gx-word").value;
        $("#gx-filllab").style.display = style.startsWith("rahmen") ? "none" : "";
        const fill = $("#gx-fill").value;
        const t = style.startsWith("block") ? blockText(word, { fill, wide: style === "block-breit" })
          : style.startsWith("banner") ? bannerText(word, { fill, size: style.split("-")[1] })
          : frameText(word, style.split("-")[1]);
        $("#gx-bhint").hidden = !style.startsWith("banner");
        $("#gx-out").textContent = t; setText(t, true);
      };
      ["#gx-word", "#gx-style", "#gx-fill"].forEach(id => $(id).oninput = run);
      run(); $("#gx-word").select();
    } else if (tab === "Foto") {
      body.innerHTML = `<div class="gx-form">
        <div class="gx-drop" id="gx-drop">Foto hierher ziehen oder <u>auswählen</u><input type="file" id="gx-file" accept="image/*" hidden></div>
        <div class="gx-row">
          <label>Breite <input type="range" id="gx-cols" min="16" max="68" value="40"> <span id="gx-colsv">40</span></label>
          <label>Kontrast <input type="range" id="gx-con" min="50" max="250" value="120"></label>
          <label>Helligkeit <input type="range" id="gx-bri" min="-40" max="40" value="0"></label>
        </div>
        <div class="gx-row">
          <label>Stil <select id="gx-mode"><option value="kraeftig">Flächen (für Fotos)</option><option value="fein">Konturen (für Logos)</option></select></label>
          <label><input type="checkbox" id="gx-inv"> Umkehren</label>
        </div></div>
        <div class="gx-paper"><pre id="gx-out">Noch kein Foto gewählt.</pre></div>
        <p class="gx-hint">Am besten wirken Motive mit klarem Umriss vor hellem Hintergrund. Das Foto bleibt in deinem Browser, nur das fertige Zeichenbild wird gesendet.</p>`;
      let img = null;
      const run = () => {
        $("#gx-colsv").textContent = $("#gx-cols").value;
        if (!img) return;
        const t = convertImage(img, { cols: +$("#gx-cols").value, contrast: $("#gx-con").value / 100, brightness: $("#gx-bri").value / 100, invert: $("#gx-inv").checked, mode: $("#gx-mode").value });
        $("#gx-out").textContent = t; setText(t, true);
      };
      const load = file => {
        if (!file || !file.type.startsWith("image/")) return;
        const url = URL.createObjectURL(file), im = new Image();
        im.onload = () => { img = im; URL.revokeObjectURL(url); run(); };
        im.src = url;
      };
      const drop = $("#gx-drop");
      drop.onclick = () => $("#gx-file").click();
      $("#gx-file").onchange = e => load(e.target.files[0]);
      drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
      drop.ondragleave = () => drop.classList.remove("over");
      drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); load(e.dataTransfer.files[0]); };
      ["#gx-cols", "#gx-con", "#gx-bri", "#gx-mode", "#gx-inv"].forEach(id => $(id).oninput = run);
    }
  }

  window.TelexGrafik = { open, blockText, bannerText, frameText, convertImage, stats, norm };
})();
