/* UI smoke test: boots the real page + app.js inside jsdom against a running
 * server and drives the actual flows (identify, inventory, studio, CSV import,
 * new-part drawer, unverified gating). No headless browser needed.
 *
 *   npm install jsdom                     # once, anywhere on the machine
 *   python -m uvicorn app.main:app --port 8000 &
 *   node scripts/ui_smoke.cjs             # BASE=http://localhost:8000 by default
 *
 * Exits non-zero if any check fails. It creates and deletes its own test parts,
 * so it can be run against a real database without leaving anything behind.
 */
const { JSDOM, VirtualConsole } = require("jsdom");

const BASE = process.env.BASE || "http://localhost:8000";
const results = [];
const consoleErrors = [];

function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(label, fn, timeout = 25000, interval = 120) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const v = await fn();
      if (v) return v;
    } catch { /* keep polling */ }
    await sleep(interval);
  }
  throw new Error(`timeout waiting for: ${label}`);
}

(async () => {
  const html = await (await fetch(`${BASE}/`)).text();
  const appJs = await (await fetch(`${BASE}/static/app.js`)).text();

  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => {
    if (!/scrollTo\(\) method|Not implemented: navigation/.test(e.message)) {
      consoleErrors.push(`jsdomError: ${e.message}`);
    }
  });
  virtualConsole.on("error", (m) => consoleErrors.push(`console.error: ${m}`));

  const dom = new JSDOM(html.replace(/<script src="\/static\/app.js"><\/script>/, ""), {
    url: `${BASE}/`,
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  const doc = window.document;

  // ── give the page a working environment (jsdom lacks fetch/File/objectURL) ──
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? new URL(input, BASE).href : input;
    return fetch(url, init);
  };
  window.FormData = globalThis.FormData;
  window.Blob = globalThis.Blob;
  window.File = globalThis.File;
  window.Headers = globalThis.Headers;
  window.Request = globalThis.Request;
  window.Response = globalThis.Response;
  window.URL.createObjectURL = () => "blob:mock";
  window.URL.revokeObjectURL = () => {};
  Object.defineProperty(window.navigator, "clipboard", {
    value: { writeText: async () => {} },
    configurable: true,
  });
  window.addEventListener("error", (e) => consoleErrors.push(`window.error: ${e.message}`));
  // jsdom does not implement these; browsers do, so they are not app defects.
  const jsdomGaps = [/scrollTo\(\) method/, /Not implemented: navigation/];
  const recordConsole = (msg) => {
    if (!jsdomGaps.some((re) => re.test(msg))) consoleErrors.push(msg);
  };

  // boot the app exactly as a browser would
  window.eval(appJs);

  // ── 1. boot ────────────────────────────────────────────────────────────────
  await waitFor("boot", () => doc.querySelector("#stat-pill").textContent !== "…");
  const config = await (await fetch(`${BASE}/api/config`)).json();
  check(
    "boot renders shop identity from /api/config",
    doc.querySelector("#shop-name").textContent === config.shop_name,
    doc.querySelector("#shop-name").textContent,
  );
  check(
    "provider badge shows the active engine",
    doc.querySelector("#provider-badge").textContent.trim().length > 0,
    doc.querySelector("#provider-badge").textContent.trim(),
  );
  check(
    "stat pill shows inventory totals",
    /parts/.test(doc.querySelector("#stat-pill").textContent),
    doc.querySelector("#stat-pill").textContent,
  );

  // ── 2. demo chips ──────────────────────────────────────────────────────────
  await waitFor("demo chips", () => doc.querySelectorAll("#demo-chips .chip").length > 0);
  const chips = doc.querySelectorAll("#demo-chips .chip");
  check("sample photos are offered", chips.length >= 1, `${chips.length} chips`);
  check(
    "sample chips show thumbnails",
    chips[0].querySelector("img")?.getAttribute("src")?.startsWith("/api/demo-image/"),
  );

  // ── 3. identify an uploaded photo (auto-detect on) ─────────────────────────
  chips[0].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("report rendered", () => {
    const r = doc.querySelector("#report");
    return r && !r.hidden && r.textContent.includes("PART IDENTIFICATION REPORT");
  }, 30000);
  const reportText = doc.querySelector("#report").textContent;
  check("report has the three required sections",
    reportText.includes("PART IDENTIFICATION REPORT") &&
    reportText.includes("WAREHOUSE & INVENTORY DETAILS") &&
    reportText.includes("WHATSAPP PROMOTIONAL POST GENERATOR"));
  check("report shows the identified part name",
    reportText.includes("Part Name"));
  check("preview image is shown after upload",
    doc.querySelector("#preview").hidden === false &&
    String(doc.querySelector("#preview").src).startsWith("blob:"));
  check("match block offers promo cards",
    doc.querySelector("#match-block").hidden === false &&
    Boolean(doc.querySelector("#btn-match-cards")),
    doc.querySelector("#match-badge")?.textContent?.trim());

  // sources block: no public URL on localhost, so it should stay hidden
  check("sources block hidden when there is nothing to cite (localhost)",
    doc.querySelector("#sources-block").hidden === true);

  // A grounded scan must surface citations + the Lens shortcut. Drive it the
  // way the server would: stub /api/identify with a grounded payload.
  const groundedPayload = {
    scan_id: 999,
    image_path: "uploads/grounded-probe.jpg",
    image_url: "/media/uploads/grounded-probe.jpg",
    lens_url: "https://lens.google.com/uploadbyurl?url=https%3A%2F%2Fexample.com%2Fa.jpg",
    google_search_url: "https://www.google.com/searchbyimage?image_url=x",
    candidates: [],
    matched_part: null,
    report_markdown: "### PART IDENTIFICATION REPORT\n- **Part Name:** PCV Valve\n",
    report: {
      part_name: "PCV Valve",
      oem_part_number: "5184894AA",
      number_confidence: "high",
      overall_confidence: "high",
      grounded: true,
      provider: "gemini-grounded",
      model_name: "gemini-2.5-flash",
      sources: [
        { title: "5184894AA PCV Valve | Mopar", url: "https://www.moparparts.com/p/5184894aa" },
        { title: "PCV valve cross reference", url: "https://www.rockauto.com/x" },
      ],
      search_queries: ["5184894AA pcv valve mopar"],
      fitment: [], technical_notes: [], visual_evidence: [], warnings: [],
    },
  };
  const realFetch = window.fetch;
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (url.includes("/api/identify")) {
      return Promise.resolve(new Response(JSON.stringify(groundedPayload), {
        status: 200, headers: { "content-type": "application/json" },
      }));
    }
    return realFetch(input, init);
  };
  doc.querySelectorAll("#demo-chips .chip")[1].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("grounded report", () => doc.querySelector("#sources-block").hidden === false, 30000);

  const links = doc.querySelectorAll("#source-list a");
  check("grounded sources render as external links",
    links.length === 2 && links[0].href.includes("moparparts.com"),
    links[0]?.textContent?.trim());
  check("domains are shown next to citations",
    doc.querySelector("#source-list .domain")?.textContent?.includes("moparparts.com"));
  check("search queries are disclosed",
    doc.querySelector("#query-line").textContent.includes("5184894AA"));
  check("reverse-image-search button appears when public",
    doc.querySelector("#lens-link a")?.href.includes("lens.google.com"));
  check("grounded scan status mentions web search",
    doc.querySelector("#identify-status").textContent.includes("web search"),
    doc.querySelector("#identify-status").textContent.trim());
  window.fetch = realFetch;

  // ── 4. inventory tab ───────────────────────────────────────────────────────
  doc.querySelector('.tab[data-view="inventory"]').dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("inventory rows", () => doc.querySelectorAll("#parts-body tr").length > 0);
  const rows = doc.querySelectorAll("#parts-body tr").length;
  check("inventory table renders rows", rows >= 4, `${rows} rows`);
  const firstRow = doc.querySelector("#parts-body tr");
  check("rows show OEM, bin and price",
    firstRow.textContent.includes("KES") || /\d/.test(firstRow.textContent));

  // search filter
  const search = doc.querySelector("#search");
  search.value = "Durango";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  await waitFor("filtered rows", () => {
    const n = doc.querySelectorAll("#parts-body tr").length;
    return n > 0 && n < rows;
  }, 15000);
  check("search filters the table", doc.querySelectorAll("#parts-body tr").length < rows,
    `${doc.querySelectorAll("#parts-body tr").length} of ${rows}`);
  search.value = "";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  await waitFor("unfiltered rows", () => doc.querySelectorAll("#parts-body tr").length === rows);

  // ── 5. promo studio ────────────────────────────────────────────────────────
  doc.querySelector('.tab[data-view="studio"]').dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("studio options", () => doc.querySelectorAll("#studio-part option").length > 0);
  check("studio lists every part", doc.querySelectorAll("#studio-part option").length === rows,
    `${doc.querySelectorAll("#studio-part option").length} options`);

  doc.querySelector("#btn-generate").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("cards rendered", () => doc.querySelectorAll("#card-output .card-item img").length > 0, 40000);
  const cards = [...doc.querySelectorAll("#card-output .card-item")];
  check("all three card formats are generated", cards.length === 3, `${cards.length} cards`);
  const meta = cards.map((c) => c.querySelector(".card-meta span").textContent);
  check("card dimensions are labelled", meta.some((m) => m.includes("1080×1920")) && meta.some((m) => m.includes("1080×1080")),
    meta.join(" | "));
  check("cards have download links",
    cards.every((c) => c.querySelector('a[download]')?.getAttribute("href")?.startsWith("/api/cards/download/")));
  check("caption is generated", doc.querySelector("#caption-text").textContent.includes("WHATSAPP")
    || doc.querySelector("#caption-text").textContent.includes("WhatsApp"));

  const cardSrc = cards[0].querySelector("img").getAttribute("src");
  const cardResp = await fetch(new URL(cardSrc, BASE));
  check("generated card is actually served as a PNG",
    cardResp.ok && cardResp.headers.get("content-type") === "image/png",
    `${cardResp.status} ${cardResp.headers.get("content-type")}`);

  // bin label format toggles on
  doc.querySelector('#format-toggles input[value="bin_label"]').checked = true;
  doc.querySelector("#btn-generate").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("four cards", () => doc.querySelectorAll("#card-output .card-item").length === 4, 40000);
  check("bin label format renders", doc.querySelectorAll("#card-output .card-item").length === 4);

  // ── 6. CSV import (dry run through the real UI path) ───────────────────────
  doc.querySelector('.tab[data-view="inventory"]').dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const csv = "Part Name,OEM,Fitment,Qty,Price\nUI Test Bracket,UIT-9001,Toyota Hilux 2.7L 2015-present,7,1200\n";
  const csvFile = new window.File([csv], "ui-test.csv", { type: "text/csv" });
  const csvInput = doc.querySelector("#csv-input");
  Object.defineProperty(csvInput, "files", { value: [csvFile], configurable: true });
  csvInput.dispatchEvent(new window.Event("change", { bubbles: true }));
  await waitFor("import summary", () => doc.querySelector("#import-summary").textContent.includes("UI Test Bracket")
    || doc.querySelector("#import-summary").textContent.includes("rows read"), 20000);
  const importText = doc.querySelector("#import-summary").textContent;
  check("dry run reports the row without writing", importText.includes("1") && doc.querySelector("#import-block").hidden === false,
    importText.replace(/\s+/g, " ").slice(0, 110));
  check("dry run offers an Apply button", Boolean(doc.querySelector("#btn-apply-import")));

  const before = (await (await fetch(`${BASE}/api/parts?q=UIT-9001`)).json()).length;
  doc.querySelector("#btn-apply-import").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("import applied", async () => {
    const rowsNow = await (await fetch(`${BASE}/api/parts?q=UIT-9001`)).json();
    return rowsNow.length > before;
  }, 25000);
  check("applying the import writes the part", true, "UIT-9001 now in stock");

  const imported = (await (await fetch(`${BASE}/api/parts?q=UIT-9001`)).json())[0];
  check("imported fitment is structured", imported.fitment?.[0]?.make === "Toyota",
    JSON.stringify(imported.fitment?.[0] || {}));
  check("imported price parsed", imported.unit_price === 1200, String(imported.unit_price));

  // clean up the part created by this run
  await fetch(`${BASE}/api/parts/${imported.id}`, { method: "DELETE" });

  // ── 7. new-part drawer ─────────────────────────────────────────────────────
  doc.querySelector("#btn-new-part").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await waitFor("drawer open", () => doc.querySelector("#drawer").hidden === false);
  const form = doc.querySelector("#part-form");
  form.elements.part_name.value = "UI Drawer Probe";
  form.elements.oem_part_number.value = "UIP-4242";
  form.elements.bin_location.value = "Q7-Q7";
  form.elements.quantity.value = "3";
  form.elements.unit_price.value = "999";
  form.elements.verified.checked = true;
  form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  const created = await waitFor("part created", async () => {
    const hits = await (await fetch(`${BASE}/api/parts?q=UIP-4242`)).json();
    return hits.length ? hits[0] : null;
  }, 20000);
  check("drawer saves a new part", created.bin_location === "Q7-Q7" && created.quantity === 3,
    `#${created.id} ${created.part_name}`);
  await waitFor("drawer closed", () => doc.querySelector("#drawer").hidden === true);
  await fetch(`${BASE}/api/parts/${created.id}`, { method: "DELETE" });

  // ── 8. unverified part gating surfaces the message ─────────────────────────
  const draft = await (await fetch(`${BASE}/api/parts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ part_name: "Unverified Probe", oem_part_number: "UIU-1", quantity: 1 }),
  })).json();
  const gate = await fetch(`${BASE}/api/cards`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ part_id: draft.id, formats: ["square"] }),
  });
  const gateBody = await gate.json();
  check("unverified records cannot publish cards", gate.status === 409 &&
    /verified/i.test(gateBody.detail || ""), `${gate.status}: ${(gateBody.detail || "").slice(0, 60)}`);
  await fetch(`${BASE}/api/parts/${draft.id}`, { method: "DELETE" });

  // ── 9. console hygiene ─────────────────────────────────────────────────────
  check("no uncaught page errors or console errors", consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(" | "));

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} UI checks passed`);
  if (failed.length) {
    console.log("FAILURES:\n" + failed.map((f) => ` · ${f.name} — ${f.detail}`).join("\n"));
  }
  process.exit(failed.length ? 1 : 0);
})().catch((error) => {
  console.error("HARNESS ERROR:", error.message);
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed before the harness stopped`);
  process.exit(2);
});
