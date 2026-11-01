// Accessibility check: opens every page of a running optlab and runs axe-core (WCAG 2.1 A + AA rules).
//   node web/scripts/a11y.mjs http://127.0.0.1:8600
// Needs Playwright (preinstalled in the dev container, or `npm i -D playwright`) and axe-core (devDependency).
// Exit code 1 when any page has a violation.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const here = dirname(fileURLToPath(import.meta.url));
const axeSource = readFileSync(join(here, "..", "node_modules", "axe-core", "axe.min.js"), "utf8");
const base = (process.argv[2] || "http://127.0.0.1:8600").replace(/\/$/, "");

const get = async (path) => (await fetch(base + "/api/v1" + path)).json();
const accounts = await get("/accounts");
const acc = accounts[0]?.id;
const pos = (await get(`/positions?account_id=${acc}&page_size=1`)).items?.[0]?.id;
const plan = (await get(`/plans?account_id=${acc}&page_size=1`)).items?.[0]?.id;
const run = (await get("/analysis/runs?limit=1"))[0]?.id;
const routes = ["/", "/plans", "/risk", "/debrief", "/journal", "/news", "/analysis", "/learn",
  pos && `/positions/${pos}`, plan && `/plans/${plan}`, run && `/analysis/${run}`, plan && `/analysis/new/plan/${plan}`].filter(Boolean);

const browser = await chromium.launch();
let total = 0;
for (const theme of ["professional", "colony"]) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, bypassCSP: true });   // the dashboard CSP blocks injected scripts, axe included
  await page.goto(base + "/#/");
  await page.waitForTimeout(800);
  const radio = page.getByRole("radio", { name: theme === "colony" ? "Colony" : "Professional" });
  if (await radio.count()) { await radio.click(); await page.waitForTimeout(800); }   // server-driven: click, then wait
  for (const r of routes) {
    await page.goto(`${base}/#${r}`);
    // not "networkidle": the live-update stream (SSE) keeps a connection open
    await page.waitForSelector("main .panel, main .detail", { timeout: 15000 }).catch(() => {});
    await page.waitForFunction(() => !document.querySelector("main .spinner"), null, { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(400);
    await page.addScriptTag({ content: axeSource });
    const res = await page.evaluate(async () => await window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] }, resultTypes: ["violations"] }));
    const v = res.violations;
    total += v.length;
    console.log(`${theme.padEnd(12)} ${r.padEnd(60)} ${v.length ? "VIOLATIONS: " + v.length : "ok"}`);
    for (const x of v) {
      console.log(`    [${x.impact}] ${x.id}: ${x.help} (${x.nodes.length} element(s))`);
      for (const n of x.nodes.slice(0, 3)) console.log(`        ${n.target.join(" ")} :: ${(n.failureSummary || "").split("\n").slice(1, 2).join(" ").trim()}`);
    }
  }
  await page.close();
}
await browser.close();
console.log(total ? `\n${total} violation group(s)` : "\nno violations");
process.exit(total ? 1 : 0);
