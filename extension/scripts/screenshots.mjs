// Makes the Chrome Web Store screenshots (1280 x 800) from the real extension on a made-up landing page, so they always show the
// current UI:   node extension/scripts/screenshots.mjs   -> extension/store/screenshots/*.png
import http from "node:http";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const ext = path.resolve(here, "..");
const { chromium } = createRequire(path.resolve(ext, "../design/package.json"))("playwright");
const outDir = path.join(ext, "store", "screenshots");
fs.mkdirSync(outDir, { recursive: true });

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fernwood Coffee</title><link rel="stylesheet" href="/site.css"></head><body>
<header class="bar"><span class="logo">Fernwood</span><nav><a href="#">Menu</a><a href="#">Stores</a><a href="#">Beans</a><a class="cta" href="#">Order now</a></nav></header>
<main><section class="hero"><p class="kicker">Small-batch roasting</p><h1 class="hero-title">Coffee worth slowing down for</h1>
<p class="lede">We roast in small batches every Tuesday and ship the same day, so what reaches your cup is days old, not weeks.</p>
<p><button class="primary">Shop this week's roast</button> <button class="ghost">How we roast</button></p></section>
<section class="cards"><article class="card"><h2>Ethiopia Guji</h2><p class="muted">Blueberry, jasmine, honey</p><span class="price">$18</span></article>
<article class="card"><h2>Colombia Huila</h2><p class="muted">Red apple, caramel, cocoa</p><span class="price">$16</span></article>
<article class="card"><h2>Sumatra Mandheling</h2><p class="muted">Cedar, dark chocolate, spice</p><span class="price">$17</span></article></section></main></body></html>`;
const CSS = `:root { --bean: #3b2a20; --cream: #f6efe6; --leaf: #5b8a5a; --accent: #d9783a; }
* { box-sizing: border-box; } body { margin: 0; font: 17px/1.5 system-ui, sans-serif; color: var(--bean); background: #fffdf9; }
.bar { display: flex; justify-content: space-between; align-items: center; padding: 18px 56px; background: var(--cream); }
.logo { font-weight: 800; font-size: 22px; letter-spacing: -0.5px; } nav { display: flex; gap: 28px; align-items: center; } nav a { color: var(--bean); text-decoration: none; }
.cta { background: var(--bean); color: #fff; padding: 8px 18px; border-radius: 6px; } main { padding: 56px; max-width: 1180px; margin: 0 auto; }
.kicker { color: var(--leaf); font-weight: 700; text-transform: uppercase; letter-spacing: 2px; font-size: 13px; margin: 0; }
.hero-title { font-size: 54px; line-height: 1.05; margin: 10px 0 16px; max-width: 14ch; } .lede { font-size: 19px; max-width: 46ch; color: #7a6a5f; }
button { font: inherit; padding: 12px 22px; border-radius: 8px; cursor: pointer; } .primary { background: var(--accent); color: #fff; border: 0; font-weight: 600; }
.ghost { background: transparent; border: 2px solid var(--bean); color: var(--bean); } .cards { display: flex; gap: 24px; margin-top: 48px; }
.card { flex: 1; background: var(--cream); padding: 24px; border-radius: 14px; } .card h2 { margin: 0 0 4px; font-size: 22px; } .muted { color: #a89a8f; margin: 0 0 14px; } .price { font-weight: 700; }`;
const server = http.createServer((req, res) => {
  res.setHeader("Content-Type", req.url === "/site.css" ? "text/css" : "text/html");
  res.end(req.url === "/site.css" ? CSS : PAGE);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const url = `http://127.0.0.1:${server.address().port}/`;

const dist = path.join(os.tmpdir(), "design-picker-shots-" + process.pid);
execFileSync("node", [path.join(ext, "build.mjs"), "--test", "--out", dist], { stdio: "inherit" });
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "design-picker-shots-profile-"));
const context = await chromium.launchPersistentContext(profile, {
  headless: false, viewport: { width: 1280, height: 800 },
  args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`],
});
try {
  const worker = context.serviceWorkers()[0] || (await context.waitForEvent("serviceworker", { timeout: 15000 }));
  const page = await context.newPage();
  await page.goto(url);
  await worker.evaluate(async () => { const [tab] = await chrome.tabs.query({ url: "http://127.0.0.1/*" }); await toggle(tab); });
  await page.locator(".dz-toggle").waitFor();
  await page.evaluate(() => chrome.storage && 0).catch(() => {});
  await page.keyboard.press("Escape");   // the first-time guide
  const shot = async (name) => { await page.waitForTimeout(250); await page.screenshot({ path: path.join(outDir, name) }); console.log("wrote", name); };
  const choose = async (selector, shift = false, at = 24) => { if (shift) await page.keyboard.down("Shift"); await page.locator(selector).click({ position: { x: at, y: at } }); if (shift) await page.keyboard.up("Shift"); };

  // 1. restyle a heading: type, with the contrast readout
  await choose(".hero-title");
  await page.keyboard.press("Enter");
  await page.locator(".dz-dock button:text-is('Font')").click();
  await page.locator(".dz-dock button:text-matches('Text colour')").click();
  await page.locator(".dz-chip[title*='--leaf']").click();
  await shot("1-restyle-with-live-contrast.png");

  // 2. several elements at once
  await page.keyboard.press("Escape"); await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
  await choose(".card:nth-of-type(1)", false, 8);   // the card itself (its padding), not the heading in it
  for (const n of [2, 3]) await choose(`.card:nth-of-type(${n})`, true, 8);
  await page.keyboard.press("Enter");
  await page.locator(".dz-dock button:text-is('Radius')").click();
  await page.locator(".dz-dock button:text-is('Round')").click();
  await page.locator(".dz-dock button:text-is('Shadow')").click();
  await page.locator(".dz-dock button:text-is('Soft')").click();
  await shot("2-select-several-change-all.png");

  // 3. the inspector, with WCAG and APCA contrast
  await page.keyboard.press("Escape"); await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
  await page.keyboard.press("i");
  await page.locator(".primary").hover();
  await shot("3-inspect-contrast-and-accessible-name.png");
  await page.keyboard.press("i");

  // 4. the hand-off
  await page.locator(".dz-dock .dz-head button:text-matches('Changes')").click();
  await shot("4-copy-the-changes-as-markdown.png");
} finally {
  await context.close();
  server.close();
  fs.rmSync(dist, { recursive: true, force: true });
  fs.rmSync(profile, { recursive: true, force: true });
}
