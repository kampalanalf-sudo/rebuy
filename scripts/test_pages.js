/**
 * Rebuy static snapshot render test.
 *
 *   npm install --silent jsdom
 *   node scripts/test_pages.js
 *
 * Loads docs/index.html and docs/admin.html in jsdom exactly as a browser would
 * (minus the network) and walks every route, asserting that the view renders
 * real content and never leaks "undefined", "NaN" or "[object Object]".
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.dirname(__dirname);
const DOCS = path.join(ROOT, "docs");

const SHOP_VIEWS = ["/dashboard", "/inbox", "/reminders", "/customers", "/segments", "/loyalty",
  "/sales", "/products", "/coupons", "/reports", "/import", "/settings", "/team", "/account"];
const ADMIN_VIEWS = ["/overview", "/shops", "/revenue", "/trials", "/messages", "/channels",
  "/growth", "/audit", "/settings"];

let pass = 0, fail = 0;
const problems = [];

function ok(name, cond, detail) {
  if (cond) { pass++; console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); }
  else { fail++; problems.push(`${name}: ${detail}`); console.log(`  \x1b[31mFAIL\x1b[0m  ${name} ${detail || ""}`); }
}

function boot(file, apiFile) {
  const html = fs.readFileSync(file, "utf8");
  // keep inline scripts, drop external ones — the shim is injected manually
  const stripped = html.replace(/<script\s+src=[^>]*><\/script>/g, "");
  const dom = new JSDOM(stripped, { url: "https://example.test/", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  // jsdom ships no fetch: back it with the local filesystem so demo.json and
  // admin.json load exactly as they would over HTTP.
  w.fetch = (url) => {
    const rel = String(url).split("?")[0];
    const file = path.join(DOCS, rel);
    return new Promise((resolve, reject) => {
      fs.readFile(file, "utf8", (err, body) => {
        if (err) return reject(new Error(`HTTP 404 ${rel}`));
        resolve({ ok: true, status: 200, headers: { get: () => "application/json" },
                  text: async () => body, json: async () => JSON.parse(body) });
      });
    });
  };
  const errors = [];
  w.addEventListener("error", (e) => errors.push(e.message || String(e.error)));
  w.console.error = (...a) => errors.push(a.join(" "));
  const api = fs.readFileSync(path.join(DOCS, apiFile), "utf8");
  w.eval(api);
  for (const s of w.document.querySelectorAll("script:not([src])")) {
    try { w.eval(s.textContent); } catch (e) { errors.push("eval: " + e.message); }
  }
  return { w, errors };
}

const SUSPECT = /undefined|NaN|\[object Object\]|Cannot read propert/;

async function sweep(label, file, apiFile, views) {
  console.log(`\n\x1b[1m${label}\x1b[0m`);
  const { w, errors } = boot(file, apiFile);
  await new Promise((r) => setTimeout(r, 500));
  for (const view of views) {
    w.location.hash = "#" + view;
    await new Promise((r) => setTimeout(r, 700));
    const app = w.document.querySelector("#app");
    const text = (app ? app.textContent : "").replace(/\s+/g, " ").trim();
    const bad = SUSPECT.test(text);
    ok(`${view.padEnd(11)} renders`, text.length > 220 && !bad,
      text.length <= 220 ? `only ${text.length} chars` : `suspect text: ${text.slice(0, 180)}`);
  }
  ok(`${label} console clean`, errors.length === 0, errors.slice(0, 3).join(" | "));
}

(async () => {
  await sweep("shop app (docs/index.html)", path.join(DOCS, "index.html"), "demo-api.js", SHOP_VIEWS);
  await sweep("admin cockpit (docs/admin.html)", path.join(DOCS, "admin.html"), "admin-api.js", ADMIN_VIEWS);

  console.log(`\n\x1b[1m${pass} passed, ${fail} failed\x1b[0m`);
  if (problems.length) { console.log("\nfailures:"); problems.forEach((p) => console.log("  - " + p)); }
  process.exit(fail ? 1 : 0);
})();
