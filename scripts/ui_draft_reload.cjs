/* Draft-recovery regression tests: simulates full page reloads (the mobile
 * tab-kill / browser-reload case) by booting fresh jsdom windows against the
 * real server while localStorage and IndexedDB persist across the "reload".
 *
 * This is deliberately NOT the same as scripts/ui_smoke.cjs, which switches
 * the app's own tabs inside one window. Here every "reload" throws away the
 * window, all in-memory state and every DOM node, exactly like a phone
 * browser that discarded the background tab. Only the persisted storage
 * survives — which is the contract drafts.js + app.js must honour.
 *
 *   npm install jsdom                     # once, anywhere on the machine
 *   python -m uvicorn app.main:app --port 8000 &
 *   node scripts/ui_draft_reload.cjs      # BASE=http://localhost:8000 by default
 *
 * Covers: tab restore, new-part draft + photo recovery, single-save guarantee,
 * edit drafts, failed-save retention (record and photo stages), close/reopen,
 * explicit discard, account isolation, logout wipe, identification-result
 * preservation, interrupted-identify safety, corrupted drafts, storage quota
 * and IndexedDB unavailability. Exits non-zero on any failure and deletes the
 * parts it creates.
 */
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM, VirtualConsole } = require("jsdom");

const BASE = process.env.BASE || "http://localhost:8000";
const ACCOUNT_A = { email: process.env.AUTH_EMAIL || "ui-a@verifix.test", password: process.env.AUTH_PASSWORD || "" };
const ACCOUNT_B = { email: process.env.AUTH_EMAIL_B || "ui-b@verifix.test", password: process.env.AUTH_PASSWORD_B || "" };

const rawFetch = globalThis.fetch;
const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(label, fn, timeout = 25000, interval = 80) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try { const v = await fn(); if (v) return v; } catch { /* keep polling */ }
    await sleep(interval);
  }
  throw new Error(`timeout waiting for: ${label}`);
}

const RUN = Date.now().toString(36).slice(-5).toUpperCase();
const createdIds = new Set();
// Server-side helpers must be authenticated too, otherwise every lookup would
// 401 and "nothing was created" assertions would pass for the wrong reason.
let serverCookie = "";
async function serverJson(url, init = {}) {
  const headers = { ...(init.headers || {}), Cookie: serverCookie };
  const response = await rawFetch(new URL(url, BASE), { ...init, headers });
  if (response.status === 401) throw new Error(`harness lost its session calling ${url}`);
  const text = await response.text();
  try { return { status: response.status, body: text ? JSON.parse(text) : null }; }
  catch { return { status: response.status, body: text }; }
}
async function createPart(body) {
  const { status, body: part } = await serverJson("/api/parts", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!part?.id) throw new Error(`harness could not create a probe part (${status})`);
  createdIds.add(part.id);
  return part;
}
async function partsMatching(oem) {
  const { body } = await serverJson(`/api/parts?q=${encodeURIComponent(oem)}&limit=1000`);
  if (!Array.isArray(body)) throw new Error(`inventory search did not return a list for ${oem}`);
  return body.filter((part) => part.oem_part_number === oem);
}
async function cleanup() {
  for (const id of [...createdIds]) {
    await serverJson(`/api/parts/${id}`, { method: "DELETE" }).catch(() => {});
    createdIds.delete(id);
  }
}

/* ── auth ─────────────────────────────────────────────────────────────────── */
async function signIn(account) {
  if (!account.password) {
    throw new Error(`Set AUTH_EMAIL/AUTH_PASSWORD (and AUTH_EMAIL_B/AUTH_PASSWORD_B for the isolation test) for dedicated local test accounts.`);
  }
  const login = await rawFetch(new URL("/api/auth/signin", BASE), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: account.email, password: account.password }),
  });
  if (!login.ok) throw new Error(`Local test login failed for ${account.email} (${login.status})`);
  const cookies = login.headers.getSetCookie().map((cookie) => cookie.split(";")[0]).filter((pair) => pair.includes("="));
  return cookies.join("; ");
}

/* ── shared storage: what survives the simulated reload ───────────────────── */
function makeSharedStorage(options = {}) {
  return {
    local: new Map(),      // localStorage entries, keyed verbatim
    idb: new Map(),        // IndexedDB photo entries, structured-cloned
    quotaFull: false,      // flip mid-session to simulate a storage quota error
    idbOpenFails: options.idbOpenFails || false,
    noIdb: options.noIdb || false,
    lastAccountKey: null,
  };
}

function accountKeyFor(email) {
  const clean = String(email).trim().toLowerCase();
  const hash = require("node:crypto").createHash("sha256").update(clean).digest("hex").slice(0, 16);
  return `${hash}.${clean}`;
}
const uiKeyFor = (email) => `verifix.drafts.v1.${accountKeyFor(email)}`;
const photoKeyFor = (email, name) => `${accountKeyFor(email)}::${name}`;
const readDraft = (shared, email) => {
  const raw = shared.local.get(uiKeyFor(email));
  if (!raw) return null;
  try { return JSON.parse(raw)?.partDraft ?? null; } catch { return "<corrupted>"; }
};
const readUi = (shared, email) => {
  const raw = shared.local.get(uiKeyFor(email));
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
};
const readScan = (shared, email) => readUi(shared, email)?.scan ?? null;

/* ── browser-API mocks jsdom does not ship ────────────────────────────────── */
function installLocalStorageMock(window, shared) {
  const store = {
    getItem: (key) => (shared.local.has(String(key)) ? shared.local.get(String(key)) : null),
    setItem: (key, value) => {
      if (shared.quotaFull) {
        const error = new Error("QuotaExceededError: storage is full");
        error.name = "QuotaExceededError";
        throw error;
      }
      shared.local.set(String(key), String(value));
    },
    removeItem: (key) => { shared.local.delete(String(key)); },
    clear: () => { shared.local.clear(); },
    key: (index) => [...shared.local.keys()][index] ?? null,
    get length() { return shared.local.size; },
  };
  Object.defineProperty(window, "localStorage", { value: store, configurable: true });
}

function installIndexedDbMock(window, shared) {
  if (shared.noIdb) {
    Object.defineProperty(window, "indexedDB", { value: undefined, configurable: true });
    return;
  }
  const later = (fn) => setTimeout(fn, 0);
  class MockRequest {
    constructor() { this.result = undefined; this.error = null; this.onsuccess = null; this.onerror = null; }
    _done(result) { this.result = result; later(() => this.onsuccess?.({ target: this })); }
    _fail(error) { this.error = error; later(() => this.onerror?.({ target: this })); }
  }
  class MockObjectStore {
    constructor(name, transaction) { this.name = name; this._tx = transaction; }
    _run(work) {
      const request = new MockRequest();
      // Like the real API: the transaction completes only after its requests.
      this._tx._enqueue(() => { request._done(work()); });
      return request;
    }
    get(key) { return this._run(() => (shared.idb.has(key) ? structuredClone(shared.idb.get(key)) : undefined)); }
    put(value, key) { return this._run(() => { shared.idb.set(key, structuredClone(value)); return key; }); }
    delete(key) { return this._run(() => { shared.idb.delete(key); return undefined; }); }
  }
  class MockTransaction {
    constructor(db, mode) {
      this.db = db; this.mode = mode;
      this.oncomplete = null; this.onerror = null; this.onabort = null;
      this._queue = []; this._running = false; this._finished = false;
    }
    objectStore(name) { return new MockObjectStore(name, this); }
    _enqueue(step) {
      this._queue.push(step);
      if (!this._running) this._drain();
    }
    _drain() {
      this._running = true;
      later(() => {
        const step = this._queue.shift();
        if (step) { try { step(); } catch (error) { this._fail(error); return; } this._drain(); return; }
        this._running = false;
        if (!this._finished) { this._finished = true; later(() => this.oncomplete?.({ target: this })); }
      });
    }
    _fail(error) {
      this._finished = true;
      later(() => { this.onerror?.({ target: this, error }); this.onabort?.({ target: this }); });
    }
  }
  class MockDatabase {
    constructor(name) { this.name = name; this.objectStoreNames = { contains: (n) => n === "photos" }; }
    transaction(storeName, mode) { return new MockTransaction(this, mode); }
    createObjectStore() { return new MockObjectStore("photos"); }
    close() {}
  }
  const mock = {
    open(name, version) {
      const request = new MockRequest();
      request.onupgradeneeded = null;
      request.onblocked = null;
      if (shared.idbOpenFails) {
        later(() => request._fail(new Error("opening blocked")));
        return request;
      }
      later(() => {
        request.result = new MockDatabase(name, version);
        request.onupgradeneeded?.({ target: request, oldVersion: 0 });
        request.onsuccess?.({ target: request });
      });
      return request;
    },
    deleteDatabase() { const request = new MockRequest(); later(() => request._done(undefined)); return request; },
  };
  Object.defineProperty(window, "indexedDB", { value: mock, configurable: true });
}

function installDataTransferMock(window) {
  class MockDataTransferItemList {
    constructor(files) { this._files = files; }
    add(file) { this._files.push(file); }
    get length() { return this._files.length; }
  }
  class MockDataTransfer {
    constructor() {
      this._files = [];
      this.items = new MockDataTransferItemList(this._files);
      Object.defineProperty(this, "files", { get: () => this._files });
    }
  }
  Object.defineProperty(window, "DataTransfer", { value: MockDataTransfer, configurable: true });
}

/* ── one "page load": fresh window, persisted storage ─────────────────────── */
let HTML = null;
let DRAFTS_JS = null;
let APP_JS = null;
let lastSession = null;
const consoleErrors = [];

async function startSession({ email, cookie, shared, confirmValue = true, fetchHandler = null }) {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => {
    if (!/Not implemented: navigation/.test(error.message)) consoleErrors.push(`jsdomError: ${error.message}`);
  });
  virtualConsole.on("error", (message) => consoleErrors.push(`console.error: ${message}`));

  const dom = new JSDOM(HTML.replace(/<script src="\/static\/(drafts|app)\.js"><\/script>/g, ""), {
    url: `${BASE}/`, runScripts: "outside-only", pretendToBeVisual: true, virtualConsole,
  });
  const { window } = dom;
  const doc = window.document;
  window.scrollTo = () => {};
  window.confirmCalls = [];
  window.confirm = (message) => { window.confirmCalls.push(String(message)); return confirmValue; };

  installLocalStorageMock(window, shared);
  installIndexedDbMock(window, shared);
  installDataTransferMock(window);
  window.FormData = globalThis.FormData;
  window.Blob = globalThis.Blob;
  window.File = globalThis.File;
  window.Headers = globalThis.Headers;
  window.Request = globalThis.Request;
  window.Response = globalThis.Response;
  window.URL.createObjectURL = () => "blob:mock";
  window.URL.revokeObjectURL = () => {};
  if (!window.crypto) Object.defineProperty(window, "crypto", { value: {}, configurable: true });
  Object.defineProperty(window.crypto, "subtle", {
    value: { digest: async (algorithm, data) => require("node:crypto").webcrypto.subtle.digest(algorithm, data) },
    configurable: true,
  });
  Object.defineProperty(window.navigator, "clipboard", { value: { writeText: async () => {} }, configurable: true });

  const calls = [];
  const baseFetch = (input, init = {}) => {
    const url = typeof input === "string" ? new URL(input, BASE) : new URL(input instanceof URL ? input.href : input.url);
    const headers = new Headers(init.headers || {});
    if (url.origin === new URL(BASE).origin && cookie) headers.set("Cookie", cookie);
    // Node's fetch cannot resolve relative URLs, so always send the absolute one.
    return rawFetch(url.href, { ...init, headers });
  };
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : (input instanceof URL ? input.href : String(input?.url || input));
    const method = String(init?.method || (typeof input === "object" && input?.method) || "GET").toUpperCase();
    calls.push({ method, url });
    if (fetchHandler) {
      const stubbed = await fetchHandler(url, method, init);
      if (stubbed) return stubbed;
    }
    if (init?.body instanceof globalThis.FormData) {
      const file = init.body.get("file");
      if (file?.arrayBuffer) calls[calls.length - 1].fileBytes = Buffer.from(await file.arrayBuffer());
    }
    return baseFetch(url, init);
  };

  window.eval(DRAFTS_JS);
  window.eval(APP_JS);
  try {
    await waitFor("app boot", () =>
      doc.querySelector("#auth-email").textContent.length > 0
      && doc.querySelector("#stat-pill").textContent !== "…", 30000);
  } catch (error) {
    // Surface why the page never came up instead of a bare timeout.
    console.error(`Boot failed for ${email}. Toast: "${doc.querySelector("#toast").textContent}"`);
    console.error(`Console output so far:\n  ${consoleErrors.slice(0, 8).join("\n  ") || "(none)"}`);
    throw error;
  }
  const session = { dom, window, doc, calls, email };
  lastSession = session;
  return session;
}

/* Mobile browsers fire pagehide/visibilitychange when a tab goes to the
 * background — the app must persist everything at that moment. */
function simulateTabSwitchAway(window) {
  window.dispatchEvent(new window.Event("pagehide"));
}

const jsonStub = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

(async () => {
  const cookieA = await signIn(ACCOUNT_A);
  serverCookie = cookieA;

  // Idempotent: remove anything an interrupted earlier run left behind.
  const { body: strays } = await serverJson("/api/parts?limit=1000");
  for (const part of Array.isArray(strays) ? strays : []) {
    if (/^(UID|UIC|UIU|UIT|UIP)-/.test(part.oem_part_number || "")) {
      await serverJson(`/api/parts/${part.id}`, { method: "DELETE" });
      console.log(`  (removed leftover test part ${part.oem_part_number})`);
    }
  }

  const page = await rawFetch(new URL("/", BASE), { headers: { Cookie: cookieA } });
  if (new URL(page.url).pathname === "/login") throw new Error("Server redirected to /login despite a valid session.");
  HTML = await page.text();
  DRAFTS_JS = await (await rawFetch(new URL("/static/drafts.js", BASE))).text();
  APP_JS = await (await rawFetch(new URL("/static/app.js", BASE))).text();
  check("drafts.js and app.js are served by the app", DRAFTS_JS.includes("VerifixDrafts") && APP_JS.includes("restorePartDraft"));

  const photoBytes = fs.readFileSync(path.join(__dirname, "..", "docs", "samples", "pcv-square.png"));
  const photoFile = (name = "draft-photo.png") => new File([photoBytes], name, { type: "image/png" });
  const setPhotoInput = (doc, window, file) => {
    const input = doc.querySelector('#part-form [name="part_image"]');
    const transfer = new window.DataTransfer();
    transfer.items.add(file);
    Object.defineProperty(input, "files", { value: transfer.files, configurable: true });
    input.dispatchEvent(new window.Event("change", { bubbles: true }));
  };
  const fillNewPartForm = (doc, values) => {
    const form = doc.querySelector("#part-form");
    const win = doc.defaultView;
    for (const [name, value] of Object.entries(values)) {
      const field = form.elements[name];
      if (!field) throw new Error(`unknown form field ${name}`);
      if (field.type === "checkbox") field.checked = Boolean(value);
      else field.value = value;
      field.dispatchEvent(new win.Event("input", { bubbles: true }));
      field.dispatchEvent(new win.Event("change", { bubbles: true }));
    }
  };
  const submitPartForm = (doc) =>
    doc.querySelector("#part-form").dispatchEvent(new doc.defaultView.Event("submit", { bubbles: true, cancelable: true }));

  /* ══ 1 + 2. tab restore, new-part draft with photo, save-once ═══════════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-A-${RUN}`;
    const first = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    const { doc, window } = first;

    // Navigate to Inventory, set search + filter + sort, open New part.
    doc.querySelector('#tab-inventory').click();
    await waitFor("inventory rows", () => doc.querySelectorAll("#parts-body tr").length > 0);
    const totalRows = doc.querySelectorAll("#parts-body tr").length;
    const search = doc.querySelector("#search");
    search.value = "pcv";
    search.dispatchEvent(new window.Event("input", { bubbles: true }));
    await sleep(400);
    doc.querySelector('[data-stock-filter="out"]').click();
    doc.querySelector("#sort-received").click();

    doc.querySelector("#btn-new-part").click();
    await waitFor("drawer open", () => doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(doc, {
      part_name: "Draft Probe PCV Valve",
      oem_part_number: oem,
      alternate_numbers: "ALT-1, ALT-2",
      superseded_numbers: "SUP-1",
      category: "Engine / Valvetrain",
      bin_location: "D2-R7",
      condition: "Used",
      quantity: "5",
      unit_price: "1250.5",
      fit_make_model: "Jeep Grand Cherokee",
      fit_engine_years: "3.6L Pentastar V6 / 2011-2021",
      technical_notes: "line one\nline two",
      image_path: "assets/seed/pcv-valve.jpg",
      verified: true,
    });
    setPhotoInput(doc, window, photoFile());
    // The blob write to IndexedDB is async; wait for it instead of racing it.
    await waitFor("photo blob written", () => shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")), 8000);

    await waitFor("draft saved indicator", () =>
      /Draft saved locally/.test(doc.querySelector("#drawer-draft-status").textContent)
      && /Photo included/.test(doc.querySelector("#drawer-draft-status").textContent), 8000);
    check("drawer shows the local-draft indicator and that it is not inventory yet",
      /not yet saved to inventory/i.test(doc.querySelector("#drawer-draft-status").textContent),
      doc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 90));

    const draft = readDraft(shared, ACCOUNT_A.email);
    check("draft persists as fields change (debounced, before any unload)",
      draft && draft.part_name === "Draft Probe PCV Valve" && draft.oem_part_number === oem
      && draft.bin_location === "D2-R7" && draft.quantity === "5" && draft.unit_price === "1250.5"
      && draft.verified === true && draft.condition === "Used"
      && draft.image_path === "assets/seed/pcv-valve.jpg"
      && draft.technical_notes === "line one\nline two"
      && draft.alternate_numbers === "ALT-1, ALT-2",
      JSON.stringify(draft ?? null).slice(0, 160));
    check("draft photo blob is stored in IndexedDB with its bytes",
      Buffer.from(shared.idb.get(photoKeyFor(ACCOUNT_A.email, "part"))?.data ?? []).equals(photoBytes),
      `idb keys: ${[...shared.idb.keys()].join(", ") || "(none)"}`);
    check("draft records the photo marker honestly (name/size)",
      draft?.photo?.name === "draft-photo.png" && draft.photo.size === photoBytes.length);

    // Edit a field and background the tab immediately: the pending debounce
    // must be flushed by pagehide, like a mobile browser killing the tab.
    const form = doc.querySelector("#part-form");
    form.elements.quantity.value = "6";
    form.elements.quantity.dispatchEvent(new window.Event("input", { bubbles: true }));
    simulateTabSwitchAway(window);
    check("pagehide flushes the pending draft edit (mobile tab-kill safety)",
      readDraft(shared, ACCOUNT_A.email)?.quantity === "6",
      `quantity=${readDraft(shared, ACCOUNT_A.email)?.quantity}`);
    check("active tab is persisted for the reload", readUi(shared, ACCOUNT_A.email)?.view === "inventory");

    // ── RELOAD: brand-new window, only storage survives ──
    const reloaded = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    const rdoc = reloaded.doc;
    check("reload restores the Inventory tab instead of Identify/home",
      rdoc.querySelector("#tab-inventory").classList.contains("active")
      && rdoc.querySelector("#view-inventory").classList.contains("active")
      && rdoc.querySelector("#workspace-title").textContent === "Your inventory");
    check("reload restores search, stock filter and date sort",
      rdoc.querySelector("#search").value === "pcv"
      && rdoc.querySelector('[data-stock-filter="out"]').getAttribute("aria-pressed") === "true"
      && rdoc.querySelector("#sort-received").textContent.includes("↓"));
    await waitFor("drawer reopened after reload", () => rdoc.querySelector("#drawer").hidden === false, 10000);
    const rform = rdoc.querySelector("#part-form");
    check("reload reopens the drawer with every text field, qty, price and checkbox",
      rform.elements.part_name.value === "Draft Probe PCV Valve"
      && rform.elements.oem_part_number.value === oem
      && rform.elements.quantity.value === "6"
      && rform.elements.unit_price.value === "1250.5"
      && rform.elements.verified.checked === true
      && rform.elements.condition.value === "Used"
      && rform.elements.bin_location.value === "D2-R7"
      && rform.elements.technical_notes.value === "line one\nline two"
      && rform.elements.image_path.value === "assets/seed/pcv-valve.jpg"
      && rform.elements.id.value === "");
    check("reload shows the Draft-restored indicator (not yet in inventory)",
      /Draft restored/i.test(rdoc.querySelector("#drawer-draft-status").textContent)
      && /not yet saved to inventory/i.test(rdoc.querySelector("#drawer-draft-status").textContent),
      rdoc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 90));
    await waitFor("photo refilled into the input", () => rform.elements.part_image.files?.[0], 8000);
    const restoredFile = rform.elements.part_image.files[0];
    check("reload recovers the selected photo into the file input (IndexedDB → Blob)",
      restoredFile.name === "draft-photo.png" && restoredFile.size === photoBytes.length
      && Buffer.from(await restoredFile.arrayBuffer()).equals(photoBytes));
    check("photo preview is rendered for the recovered photo",
      rdoc.querySelector("#part-photo-preview").hidden === false
      && /draft-photo\.png/.test(rdoc.querySelector("#part-photo-preview").textContent));

    check("recovered draft was NOT auto-submitted (no duplicate stock records)",
      (await partsMatching(oem)).length === 0
      && reloaded.calls.filter((c) => c.method === "POST" && /\/api\/parts($|\?)/.test(c.url)).length === 0);

    // Save: exactly one create + one photo upload.
    rform.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    const savedParts = await waitFor("part saved once", async () => {
      const hits = await partsMatching(oem);
      return hits.length ? hits : null;
    }, 20000);
    await waitFor("drawer closed after save", () => rdoc.querySelector("#drawer").hidden === true);
    const postCalls = reloaded.calls.filter((c) => c.method === "POST" && /\/api\/parts($|\?)/.test(c.url));
    const imageCalls = reloaded.calls.filter((c) => c.method === "POST" && /\/api\/parts\/\d+\/image/.test(c.url));
    check("saving the recovered draft creates the record exactly once",
      savedParts.length === 1 && postCalls.length === 1, `${postCalls.length} POST /api/parts`);
    check("recovered photo is uploaded by savePart (no re-pick needed)",
      imageCalls.length === 1 && imageCalls[0].fileBytes?.equals(photoBytes) === true
      && Boolean(savedParts[0].image_path),
      `image_path=${savedParts[0].image_path}`);
    check("saved values match the recovered draft",
      savedParts[0].quantity === 6 && savedParts[0].bin_location === "D2-R7" && savedParts[0].verified === true);
    await sleep(300);
    check("successful save clears the local draft (form fields)", readDraft(shared, ACCOUNT_A.email) === null);
    check("successful save deletes the stored photo blob", shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")) === false);
    createdIds.add(savedParts[0].id);
  }

  /* ══ 3. edit draft for an existing record, PATCH-once ═══════════════════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-E-${RUN}`;
    const part = await createPart({ part_name: "Edit Probe Hub", oem_part_number: oem, quantity: 2, bin_location: "A1-A1" });
    const first = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    first.doc.querySelector("#tab-inventory").click();
    await waitFor("row present", () => first.doc.querySelector(`#parts-body tr[data-id="${part.id}"]`));
    first.doc.querySelector(`#parts-body tr[data-id="${part.id}"]`).click();
    await waitFor("edit drawer", () => first.doc.querySelector("#drawer").hidden === false
      && first.doc.querySelector("#part-form").elements.id.value === String(part.id));
    fillNewPartForm(first.doc, { quantity: "9", bin_location: "B2-B2" });
    await waitFor("edit draft saved", () => readDraft(shared, ACCOUNT_A.email)?.id === String(part.id)
      && readDraft(shared, ACCOUNT_A.email)?.quantity === "9", 8000);

    const reloaded = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    const rform = reloaded.doc.querySelector("#part-form");
    await waitFor("edit drawer reopened", () => reloaded.doc.querySelector("#drawer").hidden === false, 10000);
    check("edit draft reloads with the record id and edited values",
      rform.elements.id.value === String(part.id)
      && reloaded.doc.querySelector("#drawer-title").textContent === `Part #${part.id}`
      && rform.elements.quantity.value === "9" && rform.elements.bin_location.value === "B2-B2"
      && rform.elements.part_name.value === "Edit Probe Hub");
    rform.dispatchEvent(new reloaded.window.Event("submit", { bubbles: true, cancelable: true }));
    await waitFor("update applied", async () => (await serverJson(`/api/parts/${part.id}`)).body?.quantity === 9, 20000);
    check("recovered edit draft PATCHes once and never creates a duplicate",
      reloaded.calls.filter((c) => c.method === "PATCH" && c.url.includes(`/api/parts/${part.id}`)).length === 1
      && reloaded.calls.filter((c) => c.method === "POST" && /\/api\/parts($|\?)/.test(c.url)).length === 0
      && (await partsMatching(oem)).length === 1);
    await sleep(200);
    check("successful update clears the edit draft", readDraft(shared, ACCOUNT_A.email) === null);
  }

  /* ══ 4. failed record save keeps the draft ══════════════════════════════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-F-${RUN}`;
    const first = await startSession({
      email: ACCOUNT_A.email, cookie: cookieA, shared,
      fetchHandler: (url, method) => (method === "POST" && /\/api\/parts($|\?)/.test(url)
        ? jsonStub({ detail: "simulated database failure" }, 500) : null),
    });
    first.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer", () => first.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(first.doc, { part_name: "Failed Save Probe", oem_part_number: oem, quantity: "3" });
    await waitFor("draft saved", () => readDraft(shared, ACCOUNT_A.email)?.oem_part_number === oem, 8000);
    first.doc.querySelector("#part-form").dispatchEvent(new first.window.Event("submit", { bubbles: true, cancelable: true }));
    await waitFor("failure surfaced", () => /simulated database failure/.test(first.doc.querySelector("#toast").textContent), 10000);
    check("failed save keeps the drawer open and says the draft was kept",
      first.doc.querySelector("#drawer").hidden === false
      && /draft is kept/i.test(first.doc.querySelector("#drawer-draft-status").textContent),
      first.doc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 80));
    check("failed save retains the stored draft", readDraft(shared, ACCOUNT_A.email)?.quantity === "3");

    const reloaded = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await waitFor("drawer reopened", () => reloaded.doc.querySelector("#drawer").hidden === false, 10000);
    check("draft survives reload after a failed save",
      reloaded.doc.querySelector("#part-form").elements.part_name.value === "Failed Save Probe"
      && reloaded.doc.querySelector("#part-form").elements.quantity.value === "3");
    reloaded.doc.querySelector("#part-form").dispatchEvent(new reloaded.window.Event("submit", { bubbles: true, cancelable: true }));
    const hits = await waitFor("retry succeeds", async () => { const p = await partsMatching(oem); return p.length ? p : null; }, 20000);
    check("retry after reload creates the record exactly once",
      hits.length === 1 && reloaded.calls.filter((c) => c.method === "POST" && /\/api\/parts($|\?)/.test(c.url)).length === 1);
    createdIds.add(hits[0].id);
  }

  /* ══ 5. photo-upload failure: record id is kept, retry PATCHes ══════════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-P-${RUN}`;
    const first = await startSession({
      email: ACCOUNT_A.email, cookie: cookieA, shared,
      fetchHandler: (url, method) => (method === "POST" && /\/api\/parts\/\d+\/image/.test(url)
        ? jsonStub({ detail: "simulated upload failure" }, 500) : null),
    });
    first.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer", () => first.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(first.doc, { part_name: "Photo Fail Probe", oem_part_number: oem, quantity: "1" });
    await waitFor("draft saved", () => Boolean(readDraft(shared, ACCOUNT_A.email)), 8000);
    setPhotoInput(first.doc, first.window, photoFile());
    await waitFor("photo draft", () => readDraft(shared, ACCOUNT_A.email)?.photo?.name === "draft-photo.png", 8000);
    first.doc.querySelector("#part-form").dispatchEvent(new first.window.Event("submit", { bubbles: true, cancelable: true }));
    await waitFor("upload failure surfaced", () => /simulated upload failure/.test(first.doc.querySelector("#toast").textContent), 15000);
    const created = await partsMatching(oem);
    check("record was created before the photo step failed", created.length === 1);
    createdIds.add(created[0].id);
    await waitFor("draft keeps the created record id", () => readDraft(shared, ACCOUNT_A.email)?.id === String(created[0].id), 8000);
    check("draft after photo failure holds the new id (retry must PATCH, not duplicate)",
      readDraft(shared, ACCOUNT_A.email)?.id === String(created[0].id));

    const reloaded = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await waitFor("drawer reopened", () => reloaded.doc.querySelector("#drawer").hidden === false, 10000);
    check("reload restores the edit-style draft title for the created record",
      reloaded.doc.querySelector("#drawer-title").textContent === `Part #${created[0].id}`
      && reloaded.doc.querySelector("#part-form").elements.id.value === String(created[0].id));
    await waitFor("photo recovered", () => reloaded.doc.querySelector("#part-form").elements.part_image.files?.[0], 8000);
    reloaded.doc.querySelector("#part-form").dispatchEvent(new reloaded.window.Event("submit", { bubbles: true, cancelable: true }));
    await waitFor("photo uploaded on retry", async () => Boolean((await serverJson(`/api/parts/${created[0].id}`)).body?.image_path), 20000);
    check("retry PATCHes the existing record and never POSTs a second one",
      reloaded.calls.filter((c) => c.method === "PATCH" && c.url.includes(`/api/parts/${created[0].id}`)).length === 1
      && reloaded.calls.filter((c) => c.method === "POST" && /\/api\/parts($|\?)/.test(c.url)).length === 0
      && (await partsMatching(oem)).length === 1);
  }

  /* ══ 6. closing keeps the draft; discard is explicit and confirmed ══════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-C-${RUN}`;
    const part = await createPart({ part_name: "Close Probe Arm", oem_part_number: oem, quantity: 1 });
    const session = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    const { doc, window } = session;
    doc.querySelector("#btn-new-part").click();
    await waitFor("drawer", () => doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(doc, { part_name: "Close Probe", oem_part_number: `UID-C2-${RUN}`, quantity: "2" });
    setPhotoInput(doc, window, photoFile());
    await waitFor("draft with photo", () => readDraft(shared, ACCOUNT_A.email)?.photo
      && shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")), 8000);

    doc.querySelector('#drawer [data-close]').click();
    check("closing the drawer hides it but does not destroy the draft",
      doc.querySelector("#drawer").hidden === true && Boolean(readDraft(shared, ACCOUNT_A.email)?.part_name === "Close Probe")
      && shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")));

    // Reload with the drawer closed: it must stay closed, but reopening
    // New part restores the unfinished work instead of an empty form.
    const reloaded = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    check("reload with a closed drawer does not auto-open it", reloaded.doc.querySelector("#drawer").hidden === true);
    reloaded.doc.querySelector("#btn-new-part").click();
    await waitFor("draft back", () => reloaded.doc.querySelector("#part-form").elements.part_name.value === "Close Probe", 8000);
    await waitFor("photo back", () => reloaded.doc.querySelector("#part-form").elements.part_image.files?.[0], 8000);
    check("reopening New part restores the closed-drawer draft with its photo",
      reloaded.doc.querySelector("#part-form").elements.quantity.value === "2"
      && reloaded.doc.querySelector("#part-form").elements.part_image.files[0].name === "draft-photo.png");

    // Opening a DIFFERENT record must not silently replace the draft.
    const guarded = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared, confirmValue: false });
    guarded.doc.querySelector("#tab-inventory").click();
    await waitFor("row in guarded session", () => guarded.doc.querySelector(`#parts-body tr[data-id="${part.id}"]`));
    guarded.doc.querySelector(`#parts-body tr[data-id="${part.id}"]`).click();
    await sleep(300);
    check("opening another record with an unsaved draft asks for confirmation",
      guarded.window.confirmCalls.some((message) => /unsaved new-part draft/i.test(message)),
      guarded.window.confirmCalls.join(" | ").slice(0, 90));
    check("declining the confirmation keeps the drawer closed and the draft stored",
      guarded.doc.querySelector("#drawer").hidden === true && readDraft(shared, ACCOUNT_A.email)?.part_name === "Close Probe");

    // A photo recovered without a native file setter must not follow the
    // owner into a different record.
    guarded.doc.querySelector("#btn-new-part").click();
    await waitFor("draft restored for discard", () => guarded.doc.querySelector("#part-form").elements.part_name.value === "Close Probe", 8000);
    check("discard control is visible while a draft is active", guarded.doc.querySelector("#btn-discard-draft").hidden === false);
    const cross = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared, confirmValue: true });
    cross.doc.querySelector("#btn-new-part").click();
    await waitFor("cross-record draft restored", () => cross.doc.querySelector("#part-form").elements.part_name.value === "Close Probe", 8000);
    await waitFor("cross-record photo recovered", () =>
      cross.doc.querySelector('#part-form [name="part_image"]').files?.length > 0, 8000);
    cross.doc.querySelector("#tab-inventory").click();
    await waitFor("cross-record row", () => cross.doc.querySelector(`#parts-body tr[data-id="${part.id}"]`));
    cross.doc.querySelector(`#parts-body tr[data-id="${part.id}"]`).click();
    await waitFor("other record opened", () =>
      cross.doc.querySelector("#part-form").elements.id.value === String(part.id), 8000);
    check("a recovered photo never leaks into a different record",
      cross.doc.querySelector('#part-form [name="part_image"]').files?.length === 0
      && cross.doc.querySelector("#part-photo-preview").hidden === true
      && cross.doc.querySelector("#part-form").elements.part_name.value === "Close Probe Arm"
      && readDraft(shared, ACCOUNT_A.email) === null);
    // Explicit discard (confirmed) deletes draft + photo, leaves inventory alone.
    const discardSession = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared, confirmValue: true });
    discardSession.doc.querySelector("#btn-new-part").click();
    await waitFor("discard drawer", () => discardSession.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(discardSession.doc, { part_name: "Discard Probe", oem_part_number: `UID-C3-${RUN}`, quantity: "2" });
    setPhotoInput(discardSession.doc, discardSession.window, photoFile());
    await waitFor("discard draft with photo", () => readDraft(shared, ACCOUNT_A.email)?.photo
      && shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")), 8000);
    discardSession.doc.querySelector("#btn-discard-draft").click();
    await sleep(400);
    check("explicit discard is confirmed first",
      discardSession.window.confirmCalls.some((message) => /Discard this local draft/i.test(message)));
    check("confirmed discard deletes the draft and photo but not the record",
      readDraft(shared, ACCOUNT_A.email) === null
      && shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")) === false
      && (await partsMatching(oem)).length === 1
      && discardSession.doc.querySelector("#part-form").elements.part_name.value === ""
      && discardSession.doc.querySelector("#part-photo-preview").hidden === true
      && discardSession.doc.querySelector("#drawer-draft-status").hidden === true
      && discardSession.doc.querySelector("#btn-discard-draft").hidden === true);
    // (The file field itself is checked in "a recovered photo never leaks into a
    // different record": only a photo the app recovered carries its marker, while
    // this one was injected by the harness the way a user's pick would be.)
  }

  /* ══ 7. completed identification survives the reload; no auto-rerun ═════ */
  {
    const shared = makeSharedStorage();
    const scanPayload = {
      scan_id: 4242,
      image_path: "uploads/probe-scan.jpg",
      lens_url: "",
      candidates: [],
      matched_part: null,
      report_markdown: "### PART IDENTIFICATION REPORT\n- **Part Name:** Draft Probe PCV Valve\n- **OEM:** 5184894AA\n",
      report: {
        part_name: "Draft Probe PCV Valve", oem_part_number: "5184894AA",
        number_confidence: "high", overall_confidence: "high", grounded: false,
        provider: "offline", model_name: "offline-matcher",
        sources: [], search_queries: [], fitment: [], technical_notes: [], visual_evidence: [], warnings: [],
      },
    };
    let identifyCalls = 0;
    const first = await startSession({
      email: ACCOUNT_A.email, cookie: cookieA, shared,
      fetchHandler: (url, method) => {
        if (method === "POST" && url.includes("/api/identify")) { identifyCalls += 1; return jsonStub(scanPayload); }
        return null;
      },
    });
    const fileInput = first.doc.querySelector("#file-input");
    Object.defineProperty(fileInput, "files", { value: [photoFile("scan.png")], configurable: true });
    fileInput.dispatchEvent(new first.window.Event("change", { bubbles: true }));
    await waitFor("report rendered", () => !first.doc.querySelector("#report").hidden
      && /PART IDENTIFICATION REPORT/.test(first.doc.querySelector("#report").textContent), 15000);
    check("identification ran once in the first session", identifyCalls === 1);
    check("completed scan result is persisted for the next load",
      readScan(shared, ACCOUNT_A.email)?.result?.report?.part_name === "Draft Probe PCV Valve");
    await waitFor("scan photo stored", () => shared.idb.has(photoKeyFor(ACCOUNT_A.email, "identify")), 8000);

    let reloadIdentifyCalls = 0;
    const reloaded = await startSession({
      email: ACCOUNT_A.email, cookie: cookieA, shared,
      fetchHandler: (url, method) => {
        if (method === "POST" && url.includes("/api/identify")) { reloadIdentifyCalls += 1; return jsonStub(scanPayload); }
        return null;
      },
    });
    await sleep(600);
    check("reload re-renders the completed identification without re-running it",
      !reloaded.doc.querySelector("#report").hidden
      && /Draft Probe PCV Valve/.test(reloaded.doc.querySelector("#report").textContent)
      && reloadIdentifyCalls === 0);
    check("restored scan says where it came from",
      /restored from this device/i.test(reloaded.doc.querySelector("#identify-status").textContent)
      && /not re-run/i.test(reloaded.doc.querySelector("#identify-status").textContent),
      reloaded.doc.querySelector("#identify-status").textContent.trim().slice(0, 90));
    await waitFor("scan photo preview restored", () => reloaded.doc.querySelector("#preview").hidden === false, 8000);
    check("the scan photo itself is recovered into app state",
      reloaded.doc.querySelector("#preview").hidden === false
      && reloaded.doc.querySelector("#btn-identify").disabled === false);

    // An interrupted (never-returned) identify request must not resume or
    // rerun after the reload — the restored result is the last COMPLETED one.
    const hanging = await startSession({
      email: ACCOUNT_A.email, cookie: cookieA, shared,
      fetchHandler: (url, method) => (method === "POST" && url.includes("/api/identify")
        ? new Promise(() => {}) : null),
    });
    const hangInput = hanging.doc.querySelector("#file-input");
    Object.defineProperty(hangInput, "files", { value: [photoFile("second.png")], configurable: true });
    hangInput.dispatchEvent(new hanging.window.Event("change", { bubbles: true }));
    await waitFor("identify in flight", () => /Analysing|Identifying/.test(hanging.doc.querySelector("#identify-status").textContent), 8000);
    const afterKill = await startSession({
      email: ACCOUNT_A.email, cookie: cookieA, shared,
      fetchHandler: (url, method) => {
        if (method === "POST" && url.includes("/api/identify")) { reloadIdentifyCalls += 1; return jsonStub(scanPayload); }
        return null;
      },
    });
    await sleep(600);
    check("an interrupted identification is never auto-rerun after reload", reloadIdentifyCalls === 0);
    check("the last completed report is still shown after the interrupted one",
      /Draft Probe PCV Valve/.test(afterKill.doc.querySelector("#report").textContent));
  }

  /* ══ 8. account isolation and explicit-logout wipe ══════════════════════ */
  {
    const cookieB = await signIn(ACCOUNT_B);
    const shared = makeSharedStorage();
    serverCookie = cookieA;
    const oemA = `UID-IA-${RUN}`;
    const oemB = `UID-IB-${RUN}`;

    const sessionA = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    sessionA.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer A", () => sessionA.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(sessionA.doc, { part_name: "Account A Secret Draft", oem_part_number: oemA, quantity: "7" });
    setPhotoInput(sessionA.doc, sessionA.window, photoFile());
    await waitFor("draft A stored", () => readDraft(shared, ACCOUNT_A.email)?.photo, 8000);

    const sessionB = await startSession({ email: ACCOUNT_B.email, cookie: cookieB, shared });
    await sleep(500);
    check("account B never sees account A's draft",
      readDraft(shared, ACCOUNT_B.email) === null
      && sessionB.doc.querySelector("#drawer").hidden === true
      && sessionB.doc.querySelector("#part-form").elements.part_name.value === "");
    sessionB.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer B", () => sessionB.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(sessionB.doc, { part_name: "Account B Draft", oem_part_number: oemB, quantity: "1" });
    await waitFor("draft B stored", () => readDraft(shared, ACCOUNT_B.email)?.oem_part_number === oemB, 8000);
    check("both accounts keep separate drafts on the same device",
      readDraft(shared, ACCOUNT_A.email)?.part_name === "Account A Secret Draft"
      && readDraft(shared, ACCOUNT_B.email)?.part_name === "Account B Draft"
      && shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part"))
      && !shared.idb.has(photoKeyFor(ACCOUNT_B.email, "part")));

    // Explicit sign-out wipes only the signing-out account's sensitive data.
    sessionB.doc.querySelector("#btn-signout").click();
    await waitFor("logout wiped account B", () => readDraft(shared, ACCOUNT_B.email) === null, 8000);
    check("explicit logout clears the account's draft data",
      readDraft(shared, ACCOUNT_B.email) === null && readUi(shared, ACCOUNT_B.email) === null);
    check("logout of one account leaves the other account's drafts untouched",
      readDraft(shared, ACCOUNT_A.email)?.part_name === "Account A Secret Draft"
      && shared.idb.has(photoKeyFor(ACCOUNT_A.email, "part")));

    const sessionA2 = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    sessionA2.doc.querySelector("#btn-new-part").click();
    await waitFor("draft A restored after B logout", () =>
      sessionA2.doc.querySelector("#part-form").elements.part_name.value === "Account A Secret Draft", 8000);
    check("account A's draft still restores after account B signed out",
      sessionA2.doc.querySelector("#part-form").elements.quantity.value === "7");
  }

  /* ══ 9. corrupted drafts are dropped, never applied ═════════════════════ */
  {
    const shared = makeSharedStorage();
    shared.local.set(uiKeyFor(ACCOUNT_A.email), "{not json at all");
    const session = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    check("corrupted (unparseable) storage boots to the default tab without crashing",
      session.doc.querySelector("#tab-identify").classList.contains("active")
      && session.doc.querySelector("#drawer").hidden === true);

    // Every field unusable → the draft is dropped rather than half-applied.
    shared.local.set(uiKeyFor(ACCOUNT_A.email), JSON.stringify({
      view: "inventory", drawerOpen: true,
      partDraft: { id: -3, part_name: 42, quantity: "many", unit_price: "abc", verified: "yes", technical_notes: null, savedAt: "soon" },
    }));
    const second = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await sleep(800);
    check("a wholly corrupted draft is dropped instead of opening a junk form",
      second.doc.querySelector("#drawer").hidden === true
      && second.doc.querySelector("#part-form").elements.part_name.value === ""
      && second.doc.querySelector("#tab-inventory").classList.contains("active"));

    // Some fields valid, some corrupt → keep the good, sanitize the bad.
    shared.local.set(uiKeyFor(ACCOUNT_A.email), JSON.stringify({
      view: "inventory", drawerOpen: true,
      partDraft: {
        id: -3, part_name: "Partially Corrupt Probe", quantity: "many", unit_price: "abc",
        verified: "yes", technical_notes: null, bin_location: "C3-C3", savedAt: "soon",
      },
    }));
    const partial = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await waitFor("drawer reopened with sanitized draft", () => partial.doc.querySelector("#drawer").hidden === false, 8000);
    const form = partial.doc.querySelector("#part-form");
    check("wrong-typed draft fields are sanitized, valid ones kept",
      form.elements.part_name.value === "Partially Corrupt Probe"
      && form.elements.bin_location.value === "C3-C3"
      && form.elements.quantity.value === "0" && form.elements.unit_price.value === "0"
      && form.elements.verified.checked === false && form.elements.technical_notes.value === ""
      && form.elements.id.value === "" && partial.doc.querySelector("#drawer-title").textContent === "New part");

    // A record draft pointing at a deleted record must become a new-part draft.
    const stale = await createPart({ part_name: "Stale Probe", oem_part_number: `UID-S-${RUN}`, quantity: 1 });
    shared.local.set(uiKeyFor(ACCOUNT_A.email), JSON.stringify({
      view: "inventory", drawerOpen: true,
      partDraft: { id: String(stale.id), part_name: "Stale Probe Edited", quantity: "4", savedAt: Date.now() },
    }));
    await serverJson(`/api/parts/${stale.id}`, { method: "DELETE" });
    createdIds.delete(stale.id);
    const third = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await waitFor("stale drawer reopened", () => third.doc.querySelector("#drawer").hidden === false, 10000);
    await waitFor("stale id dropped", () => third.doc.querySelector("#part-form").elements.id.value === "", 10000);
    check("a draft for a deleted record is honest and becomes a new-part draft",
      /no longer exists/i.test(third.doc.querySelector("#drawer-draft-status").textContent)
      && third.doc.querySelector("#drawer-title").textContent === "New part"
      && readDraft(shared, ACCOUNT_A.email)?.id === "",
      third.doc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 100));

    // Corrupted photo bytes: the draft returns, the photo loss is stated.
    shared.idb.set(photoKeyFor(ACCOUNT_A.email, "part"), { name: "x.png", type: "image/png", data: "not-bytes" });
    shared.local.set(uiKeyFor(ACCOUNT_A.email), JSON.stringify({
      view: "inventory", drawerOpen: true,
      partDraft: { id: "", part_name: "Photo Corrupt Probe", quantity: "1", photo: { name: "x.png", type: "image/png", size: 9 }, savedAt: Date.now() },
    }));
    const fourth = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await waitFor("corrupted-photo drawer", () => fourth.doc.querySelector("#drawer").hidden === false
      && /photo/i.test(fourth.doc.querySelector("#drawer-draft-status").textContent), 10000);
    check("a corrupted photo is never implied to be recovered",
      /could not be recovered/i.test(fourth.doc.querySelector("#drawer-draft-status").textContent)
      && fourth.doc.querySelector("#part-form").elements.part_image.files?.length === 0,
      fourth.doc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 110));
  }

  /* ══ 10. storage quota + IndexedDB unavailability degrade gracefully ════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-Q-${RUN}`;
    const session = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    session.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer", () => session.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(session.doc, { part_name: "Quota Probe", oem_part_number: oem, quantity: "2" });
    await waitFor("draft saved", () => Boolean(readDraft(shared, ACCOUNT_A.email)), 8000);
    shared.quotaFull = true;                     // storage fills up mid-session
    fillNewPartForm(session.doc, { bin_location: "Z9-Z9" });
    simulateTabSwitchAway(session.doc.defaultView);
    await sleep(300);
    check("quota errors are reported honestly, not silently",
      /memory|full|blocked/i.test(session.doc.querySelector("#drawer-draft-status").textContent),
      session.doc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 90));
    check("the app keeps working with storage full (last persisted draft intact)",
      readDraft(shared, ACCOUNT_A.email)?.part_name === "Quota Probe");
    session.doc.querySelector("#part-form").dispatchEvent(new session.window.Event("submit", { bubbles: true, cancelable: true }));
    const hits = await waitFor("save still works with storage full", async () => { const p = await partsMatching(oem); return p.length ? p : null; }, 20000);
    check("saving to inventory works even when local storage is full", hits.length === 1);
    createdIds.add(hits[0].id);

    // IndexedDB unavailable: text draft still recovers; photo loss is stated.
    const noIdbShared = makeSharedStorage({ noIdb: true });
    const noIdb = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared: noIdbShared });
    noIdb.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer", () => noIdb.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(noIdb.doc, { part_name: "No IDB Probe", oem_part_number: `UID-N-${RUN}`, quantity: "1" });
    setPhotoInput(noIdb.doc, noIdb.window, photoFile());
    await waitFor("text draft saved without IDB", () => readDraft(noIdbShared, ACCOUNT_A.email)?.part_name === "No IDB Probe", 8000);
    const noIdbReload = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared: noIdbShared });
    await waitFor("drawer reopened without IDB", () => noIdbReload.doc.querySelector("#drawer").hidden === false, 10000);
    await waitFor("photo loss reported", () => /photo/i.test(noIdbReload.doc.querySelector("#drawer-draft-status").textContent), 8000);
    check("without IndexedDB the text draft still restores and photo loss is stated",
      noIdbReload.doc.querySelector("#part-form").elements.part_name.value === "No IDB Probe"
      && /could not be recovered/i.test(noIdbReload.doc.querySelector("#drawer-draft-status").textContent),
      noIdbReload.doc.querySelector("#drawer-draft-status").textContent.trim().slice(0, 110));
  }

  /* ══ 11. desktop sanity + settings toggle ═══════════════════════════════ */
  {
    const shared = makeSharedStorage();
    const oem = `UID-D-${RUN}`;
    const session = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    const { doc } = session;
    check("draft toggle in Settings defaults to on", doc.querySelector("#keep-drafts").checked === true);
    doc.querySelector("#tab-inventory").click();
    await waitFor("inventory rows", () => doc.querySelectorAll("#parts-body tr").length > 0);
    doc.querySelector("#btn-new-part").click();
    await waitFor("drawer", () => doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(doc, { part_name: "Desktop Probe", oem_part_number: oem, quantity: "1", unit_price: "10" });
    doc.querySelector("#part-form").dispatchEvent(new session.window.Event("submit", { bubbles: true, cancelable: true }));
    const hits = await waitFor("desktop save", async () => { const p = await partsMatching(oem); return p.length ? p : null; }, 20000);
    createdIds.add(hits[0].id);
    await waitFor("drawer closed", () => doc.querySelector("#drawer").hidden === true);
    check("desktop save flow still works and clears its draft",
      hits.length === 1 && readDraft(shared, ACCOUNT_A.email) === null
      && /Added part/.test(doc.querySelector("#toast").textContent));

    // Turning drafts off deletes what is stored.
    doc.querySelector("#btn-new-part").click();
    await waitFor("drawer reopened", () => doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(doc, { part_name: "Toggle Probe", quantity: "1" });
    await waitFor("toggle draft saved", () => readDraft(shared, ACCOUNT_A.email)?.part_name === "Toggle Probe", 8000);
    doc.querySelector("#tab-settings").click();
    const keep = doc.querySelector("#keep-drafts");
    keep.checked = false;
    keep.dispatchEvent(new session.window.Event("change", { bubbles: true }));
    await sleep(400);
    check("turning off on-device drafts deletes stored drafts",
      readDraft(shared, ACCOUNT_A.email) === null
      && JSON.parse(shared.local.get("verifix.device.v1") || "{}").keepDrafts === false);
    doc.querySelector("#btn-new-part").click();
    await waitFor("drawer reopened with drafts off", () => doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(doc, { part_name: "Disabled Drafts Probe", quantity: "1" });
    await sleep(600);
    check("no new draft is written while drafts are disabled", readDraft(shared, ACCOUNT_A.email) === null);
    // The off-switch must survive a reload, and nothing may be written while off.
    keep.checked = false;
    keep.dispatchEvent(new session.window.Event("change", { bubbles: true }));
    await sleep(300);
    const offReload = await startSession({ email: ACCOUNT_A.email, cookie: cookieA, shared });
    await sleep(500);
    check("the drafts off-switch survives a reload and stops all draft writes",
      offReload.doc.querySelector("#keep-drafts").checked === false
      && offReload.doc.querySelector("#tab-identify").classList.contains("active")
      && readUi(shared, ACCOUNT_A.email) === null);
    offReload.doc.querySelector('#tab-inventory').click();
    await waitFor("inventory after tab switch with drafts off", () =>
      offReload.doc.querySelectorAll("#parts-body tr").length > 0);
    offReload.doc.querySelector("#btn-new-part").click();
    await waitFor("drawer with drafts off", () => offReload.doc.querySelector("#drawer").hidden === false);
    fillNewPartForm(offReload.doc, { part_name: "Off Switch Probe", quantity: "4" });
    simulateTabSwitchAway(offReload.window);
    await sleep(400);
    check("tab switches and form edits write nothing while drafts are off",
      readUi(shared, ACCOUNT_A.email) === null && readDraft(shared, ACCOUNT_A.email) === null
      && offReload.doc.querySelector("#drawer-draft-status").hidden === true);

    keep.checked = true;
    keep.dispatchEvent(new session.window.Event("change", { bubbles: true }));
    await sleep(200);
    check("drafts can be re-enabled", JSON.parse(shared.local.get("verifix.device.v1")).keepDrafts === true);
  }

  /* ══ console hygiene ════════════════════════════════════════════════════ */
  check("no uncaught page errors or console errors across all reloads", consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(" | "));

  await cleanup();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} draft-recovery checks passed`);
  if (failed.length) console.log("FAILURES:\n" + failed.map((f) => ` · ${f.name} — ${f.detail}`).join("\n"));
  process.exit(failed.length ? 1 : 0);
})().catch(async (error) => {
  await cleanup();
  console.error("HARNESS ERROR:", error.stack || error.message);
  if (lastSession) {
    const { doc, calls, email } = lastSession;
    console.error(`\nLast session (${email}) diagnostics:`);
    console.error(`  toast:        "${doc.querySelector("#toast")?.textContent}"`);
    console.error(`  draft status: "${doc.querySelector("#drawer-draft-status")?.textContent}"`);
    console.error(`  drawer open:  ${doc.querySelector("#drawer")?.hidden === false}`);
    console.error(`  submit disabled: ${doc.querySelector('#part-form button[type="submit"]')?.disabled}`);
    console.error(`  writes: ${calls.filter((c) => c.method !== "GET").map((c) => `${c.method} ${c.url.replace(BASE, "")}`).join(", ") || "(none)"}`);
  }
  if (consoleErrors.length) console.error(`  console: ${consoleErrors.slice(0, 5).join(" | ")}`);
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed before the harness stopped`);
  process.exit(2);
});
