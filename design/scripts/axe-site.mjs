// WCAG checks with axe-core on the real pages: bun run a11y:site
// Aim it with SITE=http://127.0.0.1:8123; add ALCHEMY_USER and ALCHEMY_PASSWORD to include the signed-in pages.
import { AxeBuilder } from "@axe-core/playwright";
import { chromium } from "playwright";
import { PASSWORD, PUBLIC_PAGES, SIGNED_IN_PAGES, SITE, USER } from "./pages.mjs";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const browser = await chromium.launch({ channel: "chrome" }).catch(() => chromium.launch());
const context = await browser.newContext();
const page = await context.newPage();
const paths = [...PUBLIC_PAGES];

if (USER && PASSWORD) {   // sign in the way a person does
  await page.goto(SITE + "/login");
  await page.getByRole("textbox", { name: "Username" }).fill(USER);
  await page.getByRole("textbox", { name: "Password" }).fill(PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  await page.waitForURL((url) => url.pathname !== "/login");
  paths.push(...SIGNED_IN_PAGES);
}

let total = 0;
for (const path of paths) {
  await page.goto(SITE + path, { waitUntil: "networkidle" });
  if (new URL(page.url()).pathname !== path) { console.log(`\n${path}: redirected to ${new URL(page.url()).pathname}, skipped`); continue; }
  const { violations } = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  total += violations.length;
  console.log(`\n${path}: ${violations.length ? `${violations.length} violation(s)` : "no violations"}`);
  for (const v of violations) {
    console.log(`  [${v.impact}] ${v.id}: ${v.help} (${v.nodes.length} element(s))  ${v.helpUrl}`);
    for (const node of v.nodes.slice(0, 3)) console.log(`      ${node.target.join(" ")}   ${node.any[0]?.message ?? ""}`);
  }
}
await browser.close();
process.exit(total ? 1 : 0);
