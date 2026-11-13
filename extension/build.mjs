// Builds the unpacked extension: node extension/build.mjs [--out DIR] [--test] [--zip]
// --zip also writes extension/design-picker-<version>.zip, the file the Chrome Web Store takes.
//
// The picker itself is NOT copied into the repository twice: dist/designer.js is static/designer.js, byte for byte, and its stylesheet is
// static/designer.css wrapped in a script (the picker draws inside a Shadow DOM and takes its CSS from there). Only the thin parts that
// differ between the game's pages and the extension (host.js, background.js, options) live in extension/.
// --test grants host access up front (and so also the all-sites permission previews need), so the end-to-end test can drive the extension without a human clicking the toolbar.
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const args = process.argv.slice(2);
const test = args.includes("--test");
const makeZip = args.includes("--zip");
const out = path.resolve(args.includes("--out") ? args[args.indexOf("--out") + 1] : path.join(here, "dist"));
const version = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8")).version;

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(path.join(out, "icons"), { recursive: true });

// ---- the picker, unchanged
fs.copyFileSync(path.join(root, "static", "designer.js"), path.join(out, "designer.js"));
const css = fs.readFileSync(path.join(root, "static", "designer.css"), "utf8");
fs.writeFileSync(path.join(out, "designer-css.js"), `window.__designerCss = ${JSON.stringify(css)};\n`);

// ---- the extension's own files
for (const file of ["host.js", "restore.js", "background.js", "options.html", "options.js"]) fs.copyFileSync(path.join(here, file), path.join(out, file));
const manifest = JSON.parse(fs.readFileSync(path.join(here, "manifest.template.json"), "utf8").replace("__VERSION__", version));
if (manifest.description.length > 132) throw new Error(`the manifest description is ${manifest.description.length} characters; the Chrome Web Store allows 132`);
if (test) manifest.host_permissions = ["<all_urls>"];   // the real build asks only for the tab clicked (activeTab), and all sites only if the designer opts in
fs.writeFileSync(path.join(out, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");

// ---- icons: the game's flask on its brown, drawn here so there is no image tool to install (Chrome wants PNGs)
const FLASK = [[26, 10], [38, 10], [38, 14], [35, 14], [35, 28], [49, 52], [48, 55], [44.7, 59.5], [19.3, 59.5], [16, 55], [15, 52], [29, 28], [29, 14], [26, 14]];
const LIQUID = [[22, 46], [42, 46], [47, 54.5], [17, 54.5]];
function inside(poly, x, y) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}
const roundedSquare = (x, y) => {
  const r = 12, cx = Math.min(Math.max(x, r), 64 - r), cy = Math.min(Math.max(y, r), 64 - r);
  return (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
};
const crcTable = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
const crc = (buf) => { let c = 0xffffffff; for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
function png(size) {
  const raw = Buffer.alloc((size * 4 + 1) * size);
  const sub = 4;   // supersampling for smooth edges
  for (let py = 0; py < size; py++) {
    raw[py * (size * 4 + 1)] = 0;
    for (let px = 0; px < size; px++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < sub; sy++) for (let sx = 0; sx < sub; sx++) {
        const x = ((px + (sx + 0.5) / sub) / size) * 64, y = ((py + (sy + 0.5) / sub) / size) * 64;
        if (!roundedSquare(x, y)) continue;
        const [cr, cg, cb] = inside(LIQUID, x, y) ? [255, 153, 51] : inside(FLASK, x, y) ? [244, 241, 232] : [107, 78, 61];
        r += cr; g += cg; b += cb; a += 255;
      }
      const n = sub * sub, o = py * (size * 4 + 1) + 1 + px * 4;
      const cover = a / 255;
      raw[o] = cover ? Math.round(r / cover) : 0; raw[o + 1] = cover ? Math.round(g / cover) : 0; raw[o + 2] = cover ? Math.round(b / cover) : 0;
      raw[o + 3] = Math.round((cover / n) * 255);
    }
  }
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const sum = Buffer.alloc(4); sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0); header.writeUInt32BE(size, 4); header[8] = 8; header[9] = 6;   // 8-bit RGBA
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", header), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}
for (const size of [16, 48, 128]) fs.writeFileSync(path.join(out, "icons", `${size}.png`), png(size));

// ---- the zip for the Chrome Web Store
function zipDirectory(dir) {
  const entries = [];
  const walk = (current, prefix) => {
    for (const name of fs.readdirSync(current).sort()) {
      const full = path.join(current, name);
      if (fs.statSync(full).isDirectory()) walk(full, prefix + name + "/");
      else entries.push({ name: prefix + name, data: fs.readFileSync(full) });
    }
  };
  walk(dir, "");
  const locals = [], centrals = [];
  let offset = 0;
  const stamp = new Date(2026, 0, 1);   // a fixed time, so the same sources always make the same zip
  const dosTime = (stamp.getHours() << 11) | (stamp.getMinutes() << 5), dosDate = ((stamp.getFullYear() - 1980) << 9) | ((stamp.getMonth() + 1) << 5) | stamp.getDate();
  for (const { name, data } of entries) {
    const packed = zlib.deflateRawSync(data), nameBuf = Buffer.from(name), sum = crc(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x0800, 6); local.writeUInt16LE(8, 8);   // deflate, UTF-8 names
    local.writeUInt16LE(dosTime, 10); local.writeUInt16LE(dosDate, 12); local.writeUInt32LE(sum, 14);
    local.writeUInt32LE(packed.length, 18); local.writeUInt32LE(data.length, 22); local.writeUInt16LE(nameBuf.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6); central.writeUInt16LE(0x0800, 8); central.writeUInt16LE(8, 10);
    central.writeUInt16LE(dosTime, 12); central.writeUInt16LE(dosDate, 14); central.writeUInt32LE(sum, 16);
    central.writeUInt32LE(packed.length, 20); central.writeUInt32LE(data.length, 24); central.writeUInt16LE(nameBuf.length, 28); central.writeUInt32LE(offset, 42);
    locals.push(local, nameBuf, packed);
    centrals.push(central, nameBuf);
    offset += local.length + nameBuf.length + packed.length;
  }
  const centralBuf = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralBuf.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralBuf, end]);
}
if (makeZip) {
  const zipPath = path.join(here, `design-picker-${version}.zip`);
  fs.writeFileSync(zipPath, zipDirectory(out));
  console.log(`Wrote ${zipPath}`);
}

console.log(`Built the extension (version ${version}${test ? ", test build" : ""}) in ${out}`);
