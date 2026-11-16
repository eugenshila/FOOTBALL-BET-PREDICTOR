/* Verifix — local draft persistence.
 *
 * Mobile browsers routinely reload or kill background tabs, so the active
 * workspace tab, an unfinished New part / Edit part form, its selected photo
 * and the last completed identification are mirrored to on-device storage:
 *
 *   localStorage  verifix.drafts.v1.<account>  → UI state, form draft, scan
 *   localStorage  verifix.device.v1            → non-sensitive device prefs
 *   IndexedDB     verifix-drafts / photos      → selected photo blobs
 *
 * Every key is scoped to the signed-in account. Nothing here is uploaded;
 * sign-out wipes the account scope. No build step, no dependencies.
 */
(function () {
  "use strict";

  const VERSION = "v1";
  const UI_PREFIX = `verifix.drafts.${VERSION}.`;
  const DEVICE_KEY = "verifix.device.v1";
  const LAST_ACCOUNT_KEY = `${DEVICE_KEY}.last-account`;
  const DB_NAME = "verifix-drafts";
  const DB_VERSION = 1;
  const PHOTO_STORE = "photos";

  /* account = "<email-hash>.<email>" — the hash groups the keys, the raw
     email keeps them debuggable on a single-owner shop device. */
  let account = readLastAccount() || "anonymous";
  let memoryStore = new Map();       // used only when localStorage is blocked
  let localStoreOk = probeLocalStorage();
  let idbOk = typeof indexedDB !== "undefined" && indexedDB !== null;
  let dbPromise = null;

  /* ── localStorage (with memory fallback + quota reporting) ─── */
  function ls() { try { return window.localStorage; } catch { return null; } }

  function probeLocalStorage() {
    try {
      const store = ls();
      if (!store) return false;
      const probe = "verifix.probe";
      store.setItem(probe, "1");
      store.removeItem(probe);
      return true;
    } catch { return false; }
  }

  function readRaw(key) {
    if (localStoreOk) {
      try { return ls().getItem(key); } catch { localStoreOk = false; }
    }
    return memoryStore.has(key) ? memoryStore.get(key) : null;
  }

  function writeRaw(key, value) {
    if (localStoreOk) {
      try { ls().setItem(key, value); return true; } catch { /* quota/blocked → memory only */ }
    }
    memoryStore.set(key, value);
    return false;
  }

  function removeRaw(key) {
    try { if (localStoreOk) ls().removeItem(key); } catch { localStoreOk = false; }
    memoryStore.delete(key);
  }

  function readJson(key) {
    const raw = readRaw(key);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { removeRaw(key); return null; }  // corrupted → drop
  }

  function writeJson(key, value) { return writeRaw(key, JSON.stringify(value)); }

  /* ── account scoping ────────────────────────────────────────── */
  async function digestHex(value) {
    try {
      if (window.crypto?.subtle) {
        const bytes = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
        return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
      }
    } catch { /* insecure context or unavailable — fall through */ }
    let hash = 5381;
    for (let i = 0; i < value.length; i += 1) hash = ((hash * 33) ^ value.charCodeAt(i)) >>> 0;
    return hash.toString(16).padStart(8, "0");
  }

  function readLastAccount() {
    try { return window.localStorage?.getItem(LAST_ACCOUNT_KEY) || null; } catch { return null; }
  }

  async function setAccount(email) {
    const clean = String(email || "").trim().toLowerCase();
    account = clean ? `${(await digestHex(clean)).slice(0, 16)}.${clean}` : "anonymous";
    if (clean) { try { ls()?.setItem(LAST_ACCOUNT_KEY, account); } catch { /* non-sensitive marker */ } }
  }

  const uiKey = () => `${UI_PREFIX}${account}`;
  const photoKey = (name) => `${account}::${name}`;

  /* ── validation: a corrupted draft is dropped, never applied ── */
  const str = (value, max) => (typeof value === "string" ? value.slice(0, max) : "");
  const VIEWS = ["identify", "inventory", "studio", "reports", "settings"];

  function sanitizeUi(raw) {
    if (!raw || typeof raw !== "object") return null;
    const ui = {};
    if (VIEWS.includes(raw.view)) ui.view = raw.view;
    ui.search = str(raw.search, 240);
    if (["all", "in", "out", "review"].includes(raw.stockFilter)) ui.stockFilter = raw.stockFilter;
    ui.receivedSort = [0, 1, -1].includes(Number(raw.receivedSort)) ? Number(raw.receivedSort) : 0;
    if (raw.scroll && typeof raw.scroll === "object") {
      ui.scroll = {};
      for (const view of VIEWS) {
        const y = Number(raw.scroll[view]);
        if (Number.isFinite(y) && y >= 0) ui.scroll[view] = Math.min(y, 100000);
      }
    }
    if (typeof raw.drawerOpen === "boolean") ui.drawerOpen = raw.drawerOpen;
    return ui;
  }

  const DRAFT_FIELDS = [
    "part_name", "oem_part_number", "alternate_numbers", "superseded_numbers",
    "category", "bin_location", "condition", "fit_make_model", "fit_engine_years",
    "technical_notes", "image_path",
  ];

  function sanitizeDraft(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const draft = { id: "" };
    const id = Number(raw.id);
    if (Number.isInteger(id) && id > 0) draft.id = String(id);
    for (const field of DRAFT_FIELDS) draft[field] = str(raw[field], field === "technical_notes" ? 20000 : 2000);
    const quantity = Number(raw.quantity);
    draft.quantity = String(Number.isFinite(quantity) ? Math.max(0, Math.trunc(quantity)) : 0);
    const price = Number(raw.unit_price);
    draft.unit_price = String(Number.isFinite(price) ? Math.max(0, price) : 0);
    draft.verified = raw.verified === true;
    draft.savedAt = Number.isFinite(Number(raw.savedAt)) ? Number(raw.savedAt) : Date.now();
    if (raw.photo && typeof raw.photo === "object") {
      draft.photo = { name: str(raw.photo.name, 240), type: str(raw.photo.type, 120), size: Number(raw.photo.size) || 0 };
    }
    return draft;
  }

  function draftIsEmpty(draft) {
    if (!draft) return true;
    if (draft.id || draft.verified || draft.photo) return false;
    if (Number(draft.quantity) > 0 || Number(draft.unit_price) > 0) return false;
    return DRAFT_FIELDS.every((field) => !String(draft[field] || "").trim());
  }

  /* A stored scan is { savedAt, result } where result is the /api/identify
     response. Anything without a report object is treated as corrupt. */
  function sanitizeScan(raw) {
    if (!raw || typeof raw !== "object") return null;
    const result = raw.result;
    if (!result || typeof result !== "object" || !result.report || typeof result.report !== "object") return null;
    return { savedAt: Number.isFinite(Number(raw.savedAt)) ? Number(raw.savedAt) : Date.now(), result };
  }

  /* ── IndexedDB (promise wrapper; disabled cleanly when absent) ─ */
  function openDb() {
    if (!idbOk) return Promise.resolve(null);
    if (!dbPromise) {
      dbPromise = new Promise((resolve) => {
        try {
          const request = indexedDB.open(DB_NAME, DB_VERSION);
          request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(PHOTO_STORE)) db.createObjectStore(PHOTO_STORE);
          };
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => { idbOk = false; resolve(null); };
          request.onblocked = () => { idbOk = false; resolve(null); };
        } catch { idbOk = false; resolve(null); }
      });
    }
    return dbPromise;
  }

  function idbRun(mode, fn) {
    return openDb().then((db) => {
      if (!db) return null;
      return new Promise((resolve) => {
        try {
          const tx = db.transaction(PHOTO_STORE, mode);
          const request = fn(tx.objectStore(PHOTO_STORE));
          tx.oncomplete = () => resolve(request ? request.result : undefined);
          tx.onabort = () => resolve(null);
          tx.onerror = () => resolve(null);
        } catch { resolve(null); }
      });
    }, () => null);
  }

  function packPhoto(file) {
    // Plain structured-clone data (no Blob/File) so the value survives every
    // IndexedDB implementation, including test shims.
    return file.arrayBuffer().then((buffer) => ({
      name: String(file.name || "photo"),
      type: String(file.type || ""),
      size: Number(file.size) || buffer.byteLength,
      data: new Uint8Array(buffer),
      savedAt: Date.now(),
    }));
  }

  /* Bytes may come back from a different JS realm (an iframe, a storage shim,
     a future browser quirk), where `instanceof Uint8Array` is false even for a
     genuine typed array. Detect by internal slot instead of by constructor. */
  function asBytes(data) {
    if (!data) return null;
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    if (Array.isArray(data)) return new Uint8Array(data);
    return null;
  }

  function unpackPhoto(value) {
    if (!value || typeof value !== "object") return null;
    const bytes = asBytes(value.data);
    if (!bytes || bytes.byteLength === 0) return null;
    try { return new File([bytes], value.name || "photo", { type: value.type || "application/octet-stream" }); }
    catch { try { return new Blob([bytes], { type: value.type || "" }); } catch { return null; } }
  }

  async function putPhoto(name, file) {
    if (!idbOk || !draftsEnabled() || typeof file?.arrayBuffer !== "function") return false;
    try {
      const packed = await packPhoto(file);
      const result = await idbRun("readwrite", (store) => store.put(packed, photoKey(name)));
      return result !== null;
    } catch { return false; }
  }

  async function getPhoto(name) {
    if (!idbOk) return null;
    try { return unpackPhoto(await idbRun("readonly", (store) => store.get(photoKey(name)))); }
    catch { return null; }
  }

  async function removePhoto(name) {
    if (!idbOk) return;
    try { await idbRun("readwrite", (store) => store.delete(photoKey(name))); } catch { /* best effort */ }
  }

  /* ── public draft API ───────────────────────────────────────── */
  function loadUi() { return sanitizeUi(readJson(uiKey())); }

  /* The one stored object holds the UI state *and* the part draft *and* the
     last scan. saveUi() must merge, never replace, or it would erase the
     draft that capturePartDraft() wrote a moment earlier. */
  function saveUi(patch) {
    if (!draftsEnabled()) return { persisted: false, ui: sanitizeUi(patch) };
    const existing = readJson(uiKey()) || {};
    const next = sanitizeUi({ ...(sanitizeUi(existing) || {}), ...patch }) || {};
    return { persisted: writeJson(uiKey(), { ...existing, ...next }), ui: next };
  }

  function loadPartDraft() {
    const draft = sanitizeDraft(readJson(uiKey())?.partDraft ?? null);
    return draftIsEmpty(draft) ? null : draft;
  }

  function savePartDraft(draft) {
    const clean = sanitizeDraft(draft);
    if (!clean || draftIsEmpty(clean)) return clearPartDraft();
    if (!draftsEnabled()) return { persisted: false, draft: clean };
    return { persisted: writeJson(uiKey(), { ...(readJson(uiKey()) || {}), partDraft: clean }), draft: clean };
  }

  /* Deleting must never resurrect a record: when the draft was the only thing
     stored, the key is removed outright (otherwise an empty {} would linger and
     look like saved state after the owner turned drafts off or signed out). */
  function dropSection(section) {
    const existing = readJson(uiKey()) || {};
    delete existing[section];
    if (Object.keys(existing).length === 0) removeRaw(uiKey());
    else writeJson(uiKey(), existing);
  }

  function clearPartDraft() {
    dropSection("partDraft");
    return removePhoto("part").then(() => ({ persisted: true, cleared: true }));
  }

  function loadScan() { return sanitizeScan(readJson(uiKey())?.scan ?? null); }

  function saveScan(result) {
    const clean = sanitizeScan({ savedAt: Date.now(), result });
    if (!clean || !draftsEnabled()) return { persisted: false };
    return { persisted: writeJson(uiKey(), { ...(readJson(uiKey()) || {}), scan: clean }) };
  }

  function clearScan() {
    dropSection("scan");
    return removePhoto("identify");
  }

  function draftsEnabled() {
    const device = readJson(DEVICE_KEY);
    return device?.keepDrafts !== false;
  }

  function setDraftsEnabled(enabled) {
    writeJson(DEVICE_KEY, { ...(readJson(DEVICE_KEY) || {}), keepDrafts: enabled === true });
    if (!enabled) return clearAccount();
    return Promise.resolve();
  }

  /* Explicit logout / "do not keep drafts": wipe everything for this account. */
  async function clearAccount() {
    removeRaw(uiKey());
    for (const name of ["part", "identify"]) { try { await removePhoto(name); } catch { /* best effort */ } }
    memoryStore.clear();
  }

  function storageStatus() {
    return { localStorage: localStoreOk, indexedDB: idbOk, account, anonymous: account === "anonymous" };
  }

  window.VerifixDrafts = {
    version: VERSION,
    setAccount,
    loadUi, saveUi,
    loadPartDraft, savePartDraft, clearPartDraft,
    loadScan, saveScan, clearScan,
    putPhoto, getPhoto, removePhoto,
    draftsEnabled, setDraftsEnabled,
    clearAccount,
    storageStatus,
    _internal: { uiKey, photoKey, sanitizeDraft, sanitizeUi, sanitizeScan, draftIsEmpty },
  };
})();
