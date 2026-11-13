// Performance, accessibility, best practices and SEO with Lighthouse: bun run lighthouse   (reports go to design/reports/)
import fs from "node:fs";
import lighthouse from "lighthouse";
import { launch } from "chrome-launcher";
import { PUBLIC_PAGES, SITE } from "./pages.mjs";

fs.mkdirSync("reports", { recursive: true });
const chrome = await launch({ chromeFlags: ["--headless=new"] });
const rows = [];
try {
  for (const path of PUBLIC_PAGES) {
    const result = await lighthouse(SITE + path, {
      port: chrome.port, output: ["html", "json"], logLevel: "error",
      onlyCategories: ["performance", "accessibility", "best-practices", "seo"],
    });
    const name = (path === "/" ? "home" : path.slice(1).replace(/\//g, "-"));
    fs.writeFileSync(`reports/${name}.html`, result.report[0]);
    const scores = Object.fromEntries(Object.entries(result.lhr.categories).map(([k, c]) => [k, Math.round((c.score ?? 0) * 100)]));
    rows.push({ page: path, ...scores });
  }
} finally {
  await chrome.kill();
}
console.table(rows);
console.log("Full reports: design/reports/*.html");
