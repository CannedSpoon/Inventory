"use strict";

// Label rendering for Brother P-touch tape printers such as the PT-E550W:
// 180 dpi with a 128-dot print head, so a label is drawn at exactly the
// printable height of the tape, in printer dots, as black and white.
const Labels = (() => {
  const DPI = 180;
  const MM = DPI / 25.4; // dots per mm
  // Printable height in dots for each tape width in mm.
  const TAPES = { 24: 128, 18: 112, 12: 70, 9: 50 };
  const MAX_TEXT_MM = 60;
  const FONT = "Arial, Helvetica, sans-serif";

  // Code 128 bar/space widths for symbol values 0-106 (106 is Stop).
  const C128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 " +
    "122231 113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 " +
    "322112 322211 212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 " +
    "112133 112331 132131 113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 " +
    "331121 312113 312311 332111 314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 " +
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 111242 121142 121241 114212 " +
    "124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 114311 411113 " +
    "411311 113141 114131 311141 411131 211412 211214 211232 2331112"
  ).split(" ");

  // Encode text as Code 128 (code set B). Returns module widths, alternating bar/space.
  function code128(text) {
    const values = [104];
    for (const ch of text) {
      const c = ch.charCodeAt(0);
      if (ch.length !== 1 || c < 32 || c > 126) throw new Error(`“${text}” has characters a Code 128 barcode can't hold. Use a QR code.`);
      values.push(c - 32);
    }
    values.push(values.reduce((sum, v, i) => sum + v * Math.max(i, 1), 0) % 103, 106);
    return values.flatMap((v) => [...C128[v]].map(Number));
  }

  function qrMatrix(text) {
    qrcode.stringToBytes = qrcode.stringToBytesFuncs["UTF-8"];
    const qr = qrcode(0, "L");
    qr.addData(text, "Byte");
    qr.make();
    return qr;
  }

  function fit(ctx, text, max) {
    if (ctx.measureText(text).width <= max) return text;
    let s = text;
    while (s.length > 1 && ctx.measureText(s + "…").width > max) s = s.slice(0, -1);
    return s + "…";
  }

  // label: {code, title, subtitle, link}. opts: {tape: 24|18|12|9, type: "qr"|"code128"}.
  function render(label, opts) {
    const H = TAPES[opts.tape] || TAPES[24];
    const pad = Math.round(H * 0.08);
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");

    // Work out the graphic and its size first.
    let graphicW, drawGraphic, codeUnderBars = false;
    if (opts.type === "code128") {
      const modules = code128(label.code);
      const unit = H >= 100 ? 2 : H >= 70 ? 2 : 1; // 0.28 mm bars where the tape allows
      const quiet = 10 * unit;
      const barsH = Math.round(H * 0.72);
      graphicW = quiet * 2 + modules.reduce((a, b) => a + b, 0) * unit;
      codeUnderBars = true;
      drawGraphic = (x) => {
        let bx = x + quiet;
        modules.forEach((w, i) => {
          if (i % 2 === 0) ctx.fillRect(bx, 0, w * unit, barsH);
          bx += w * unit;
        });
        ctx.font = `bold ${Math.round(H * 0.22)}px ${FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillText(label.code, x + graphicW / 2, H);
        ctx.textAlign = "left";
      };
    } else {
      const qr = qrMatrix(label.link || label.code);
      const n = qr.getModuleCount();
      const unit = Math.max(1, Math.floor(H / n));
      const size = n * unit;
      const top = Math.floor((H - size) / 2);
      graphicW = size;
      drawGraphic = (x) => {
        for (let r = 0; r < n; r++)
          for (let c = 0; c < n; c++) if (qr.isDark(r, c)) ctx.fillRect(x + c * unit, top + r * unit, unit, unit);
      };
    }

    // Text lines beside the graphic: code (unless it's under the bars), title, subtitle.
    const lines = [];
    if (!codeUnderBars) lines.push({ text: label.code, font: `bold ${Math.round(H * 0.26)}px ${FONT}` });
    if (label.title) lines.push({ text: label.title, font: `bold ${Math.round(H * 0.21)}px ${FONT}` });
    if (label.subtitle) lines.push({ text: label.subtitle, font: `${Math.round(H * 0.17)}px ${FONT}` });
    const maxText = MAX_TEXT_MM * MM;
    let textW = 0;
    for (const line of lines) {
      ctx.font = line.font;
      line.text = fit(ctx, String(line.text), maxText);
      line.width = ctx.measureText(line.text).width;
      line.size = parseInt(line.font.match(/(\d+)px/)[1], 10);
      textW = Math.max(textW, line.width);
    }
    const gap = lines.length ? Math.round(H * 0.12) : 0;

    canvas.width = Math.ceil(pad + graphicW + gap + textW + pad);
    canvas.height = H;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, H);
    ctx.fillStyle = "#000";
    drawGraphic(pad);

    const blockH = lines.reduce((h, l) => h + l.size * 1.15, 0);
    let y = (H - blockH) / 2;
    ctx.textBaseline = "top";
    for (const line of lines) {
      ctx.font = line.font;
      ctx.fillText(line.text, pad + graphicW + gap, y + line.size * 0.07);
      y += line.size * 1.15;
    }

    // Thermal printers print pure black or white: threshold the anti-aliased text.
    const img = ctx.getImageData(0, 0, canvas.width, H);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = img.data[i] < 150 ? 0 : 255;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return canvas;
  }

  // --- PNG with a 180 dpi pHYs chunk, so P-touch Editor imports it at the right size ---

  const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  function crc32(bytes) {
    let c = 0xffffffff;
    for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  async function png(canvas) {
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    const src = new Uint8Array(await blob.arrayBuffer());
    const ppm = Math.round(DPI / 0.0254); // pixels per metre
    const chunk = new Uint8Array(21);
    const view = new DataView(chunk.buffer);
    view.setUint32(0, 9);
    chunk.set([0x70, 0x48, 0x59, 0x73], 4); // "pHYs"
    view.setUint32(8, ppm);
    view.setUint32(12, ppm);
    chunk[16] = 1; // unit: metre
    view.setUint32(17, crc32(chunk.subarray(4, 17)));
    const IHDR_END = 8 + 25; // signature + IHDR chunk
    const out = new Uint8Array(src.length + chunk.length);
    out.set(src.subarray(0, IHDR_END));
    out.set(chunk, IHDR_END);
    out.set(src.subarray(IHDR_END), IHDR_END + chunk.length);
    return new Blob([out], { type: "image/png" });
  }

  function mm(dots) {
    return (dots / MM).toFixed(2);
  }

  return { TAPES, render, png, code128, mm };
})();
