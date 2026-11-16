/* Verifix — vanilla JS, no build step, no external calls. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  config: null,
  parts: [],
  stockFilter: "all",
  selectedFile: null,
  lastScan: null,
  currentPart: null,
  receivedSort: 0,
  view: "identify",
  // Selected drawer photo recovered from a draft (file inputs cannot always be
  // repopulated, so savePart() falls back to this when the input is empty).
  partDraftFile: null,
};

/* Local draft persistence (see drafts.js). Mobile browsers reload or kill
   background tabs; these flags keep restores from re-saving or auto-submitting. */
const drafts = window.VerifixDrafts || null;
let suspendDraftCapture = false;   // programmatic fills must not look like typing
let draftSavedState = false;       // a successful inventory save clears the draft
let partDraftTimer = null;
let flushPartDraftOnHide = false;

/* ── tiny helpers ─────────────────────────────────────────── */
const money = (amount, currency) => {
  const n = Number(amount || 0);
  const formatted = n.toLocaleString("en-KE", {
    minimumFractionDigits: Number.isInteger(n) ? 0 : 2,
    maximumFractionDigits: 2,
  });
  return `${currency || ""} ${formatted}`.trim();
};

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));

const SOCIAL_PLATFORMS = [
  ["facebook", "Facebook"], ["instagram", "Instagram"], ["tiktok", "TikTok"], ["x", "X"],
];

function populateShopSettings(profile) {
  const form = $("#shop-settings-form");
  if (!form || !profile) return;
  for (const key of ["shop_name", "tagline", "whatsapp_number", "currency", "location"]) {
    form.elements[key].value = profile[key] || "";
  }
  for (const [key] of SOCIAL_PLATFORMS) {
    form.elements[key].value = profile.social_links?.[key] || "";
  }
  renderSocialLinks($("#settings-social-links"), profile.social_links);
}

function renderSocialLinks(container, links = {}) {
  if (!container) return;
  const active = SOCIAL_PLATFORMS.filter(([key]) => links[key]);
  container.innerHTML = active.map(([key, label]) =>
    `<a class="btn ghost small-btn" href="${escapeHtml(links[key])}" target="_blank" rel="noopener noreferrer">Open ${label}</a>`
  ).join("");
  container.hidden = active.length === 0;
}

async function saveShopSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#btn-save-settings");
  const status = $("#settings-status");
  const values = new FormData(form);
  const payload = {
    shop_name: values.get("shop_name").trim(),
    tagline: values.get("tagline").trim(),
    whatsapp_number: values.get("whatsapp_number").trim(),
    currency: values.get("currency").trim(),
    location: values.get("location").trim(),
    social_links: Object.fromEntries(SOCIAL_PLATFORMS.map(([key]) => [key, values.get(key).trim()])),
  };
  button.disabled = true;
  button.textContent = "Saving…";
  status.textContent = "";
  try {
    const profile = await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    state.config = { ...state.config, ...profile };
    $("#shop-name").textContent = profile.shop_name;
    $("#shop-tagline").textContent = profile.tagline;
    renderSocialLinks($("#settings-social-links"), profile.social_links);
    renderSocialLinks($("#studio-social-links"), profile.social_links);
    status.className = "status";
    status.textContent = "Saved on this computer. New promo captions will use these details.";
    toast("Shop details saved");
  } catch (error) {
    status.className = "status error";
    status.textContent = error.message;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Save details";
  }
}

// Remote catalog thumbnails can expire or block hotlinking. Keep the inventory
// usable and give the owner a clear, replaceable placeholder instead.
document.addEventListener("error", (event) => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement) || img.dataset.fallbackApplied) return;
  img.dataset.fallbackApplied = "true";
  const fallback = document.createElement("span");
  fallback.className = `image-fallback${img.classList.contains("match-photo") ? " large" : ""}${img.closest(".card-item") ? " card-fallback" : ""}`;
  fallback.textContent = "Photo unavailable";
  if (img.closest(".part-photo-link")) img.closest(".part-photo-link").replaceWith(fallback);
  else img.replaceWith(fallback);
}, true);

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) {
    // An expired session is not an explicit logout: local drafts stay on the
    // device (account-scoped) and return after signing in again. Only the
    // Sign out button wipes them.
    window.location.assign("/login");
    throw new Error("Please sign in to your local account.");
  }
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : `request failed (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/* Minimal markdown renderer for the report format (headings, lists, bold,
   inline code, fenced blocks, rules). Avoids shipping a markdown library. */
function renderMarkdown(markdown) {
  const lines = String(markdown || "").split("\n");
  const out = [];
  let inFence = false;
  let inList = false;
  const inline = (text) =>
    escapeHtml(text)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/_([^_]+)_/g, "<em>$1</em>")
      .replace(/(⚠️)/g, '<span class="conf-badge conf-low">check</span>');

  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (line.startsWith("```")) {
      if (inFence) { out.push("</pre>"); inFence = false; }
      else { closeList(); out.push('<pre class="caption">'); inFence = true; }
      continue;
    }
    if (inFence) { out.push(escapeHtml(raw)); continue; }
    if (!line.trim()) { closeList(); continue; }
    if (/^#{1,6}\s/.test(line)) {
      closeList();
      out.push(`<h3>${inline(line.replace(/^#{1,6}\s*/, ""))}</h3>`);
      continue;
    }
    if (/^---+\s*$/.test(line)) { closeList(); out.push("<hr />"); continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`);
      continue;
    }
    closeList();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  if (inFence) out.push("</pre>");
  return out.join("\n");
}

/* ── boot ─────────────────────────────────────────────────── */
async function boot() {
  try {
    // The session decides which account's local drafts may be read, so it is
    // fetched before anything is restored.
    const session = await api("/api/auth/session");
    if (drafts) await drafts.setAccount(session.email || "");
    const [config, health, stats] = await Promise.all([
      api("/api/config"), api("/api/health"), api("/api/stats"),
    ]);
    $("#auth-email").textContent = session.email || "";
    state.config = config;
    try {
      restoreUiDraft();
    } catch {
      /* a corrupted UI draft must never break boot */
    }
    populateShopSettings(config);
    renderSocialLinks($("#studio-social-links"), config.social_links);
    $("#shop-name").textContent = config.shop_name;
    document.title = `${config.shop_name} — Parts workspace`;
    $("#shop-tagline").textContent = config.tagline;
    $("#max-mb").textContent = config.max_upload_mb;
    renderProviderBadge(health);
    renderStats(stats);
    await loadParts();
    await loadDemoImages();
    populateStudioSelect();
    // Drafts are restored last: the form never auto-submits, and the scan
    // report is re-rendered locally instead of re-running identification.
    try { restoreScanDraft(); } catch { /* corrupted scan draft → ignored */ }
    try { restorePartDraft()?.catch?.(() => {}); } catch { /* corrupted form draft → ignored */ }
  } catch (error) {
    toast(`Could not reach the API: ${error.message}`, true);
  }
}

function renderProviderBadge(health) {
  const badge = $("#provider-badge");
  const provider = health.vision_provider;
  const isOffline = provider === "offline";
  const grounded = Boolean(health.grounding_active);
  badge.textContent = health.vision_label || provider;
  badge.className = `badge ${isOffline ? "offline" : "live"}`;
  badge.title = isOffline
    ? "No vision key configured — identification compares the photo against your catalogue images. Add GEMINI_API_KEY (free tier) to enable Gemini + Google Search identification, or run Ollama for a local model."
    : grounded
      ? `Gemini is identifying the part with Google Search grounding (${health.grounding_model}) and citing the pages it used. Confirm numbers in the EPC before quoting.`
      : `Vision model: ${provider}. Part numbers still need EPC verification before quoting.`;
}

function renderStats(stats) {
  $("#stat-pill").textContent =
    `${stats.parts} parts · ${stats.units} units · ${money(stats.inventory_value, state.config?.currency)}`;
  $("#metric-parts").textContent = stats.parts.toLocaleString("en-KE");
  $("#metric-units").textContent = stats.units.toLocaleString("en-KE");
  $("#metric-value").textContent = money(stats.inventory_value, state.config?.currency);
  $("#metric-out").textContent = stats.out_of_stock.toLocaleString("en-KE");
  $("#metric-out").closest(".stat-card").classList.toggle("needs-stock", stats.out_of_stock > 0);
  $("#metric-out-note").textContent = stats.out_of_stock ? "Parts ready for a restock" : "No empty stock lines";
}

async function loadDemoImages() {
  const chips = $("#demo-chips");
  try {
    const images = await api("/api/demo-images");
    if (!images.length) { $("#demo-block").hidden = true; return; }
    chips.innerHTML = images.map((image) => `
      <button type="button" class="chip" data-url="${escapeHtml(image.url)}">
        <img src="${escapeHtml(image.url)}" alt="" />${escapeHtml(image.name)}
      </button>`).join("");
    chips.addEventListener("click", async (event) => {
      const chip = event.target.closest(".chip");
      if (!chip) return;
      const response = await fetch(chip.dataset.url);
      const blob = await response.blob();
      selectFile(new File([blob], `${chip.textContent.trim().replace(/\s+/g, "-")}.jpg`, { type: blob.type }));
    });
  } catch {
    $("#demo-block").hidden = true;
  }
}

/* ── capture ──────────────────────────────────────────────── */
let identifyToken = 0;

function autoDetectEnabled() {
  const toggle = $("#auto-detect");
  return Boolean(toggle && toggle.checked);
}

function selectFile(file, options = {}) {
  state.selectedFile = file;
  // Mirror the capture into IndexedDB so a mobile tab-kill does not lose the
  // photo of a finished identification. Restores never re-save or re-identify.
  if (drafts && drafts.draftsEnabled() && !options.restored && typeof file?.arrayBuffer === "function") {
    drafts.putPhoto("identify", file).catch(() => {});
  }
  const preview = $("#preview");
  preview.src = URL.createObjectURL(file);
  preview.hidden = false;
  $("#dropzone-idle").hidden = true;
  $("#btn-identify").disabled = false;
  $("#identify-status").textContent = `${file.name || "photo"} ready — ${(file.size / 1024).toFixed(0)} KB`;
  $("#identify-status").className = "status";

  if (autoDetectEnabled()) {
    // Auto-detect: identification starts the moment a photo lands. The token
    // guards against a fast second upload racing the first response.
    identify();
  }
}

function wireDropzone() {
  const zone = $("#dropzone");
  const input = $("#file-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => { if (input.files[0]) selectFile(input.files[0]); });
  ["dragenter", "dragover"].forEach((type) =>
    zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((type) =>
    zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (file) selectFile(file);
  });

  $("#btn-camera").addEventListener("click", () => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = "image/*";
    picker.capture = "environment";
    picker.addEventListener("change", () => { if (picker.files[0]) selectFile(picker.files[0]); });
    picker.click();
  });

  $("#btn-identify").addEventListener("click", identify);
}

async function identify() {
  if (!state.selectedFile) return;
  const token = ++identifyToken;
  const status = $("#identify-status");
  const grounded = state.config?.vision?.grounding_active;
  status.className = "status busy";
  status.textContent = grounded
    ? "Identifying and searching the web for part numbers…"
    : "Analysing the photo…";
  $("#btn-identify").disabled = true;

  const form = new FormData();
  form.append("file", state.selectedFile);

  try {
    const result = await api("/api/identify", { method: "POST", body: form });
    if (token !== identifyToken) return; // a newer upload superseded this one
    state.lastScan = result;
    if (drafts && drafts.draftsEnabled()) {
      try { drafts.saveScan(result); } catch { /* storage full — the report still renders */ }
    }
    renderReport(result);
    renderSources(result);
    renderMatch(result);
    status.className = "status";
    const conf = result.report.overall_confidence;
    const via = result.report.grounded ? `${result.report.provider} + web search` : result.report.provider;
    status.textContent = `Done — ${conf} confidence via ${via}.`;
    await loadParts();
    populateStudioSelect();
  } catch (error) {
    if (token !== identifyToken) return;
    status.className = "status error";
    status.textContent = error.message;
    toast(error.message, true);
  } finally {
    if (token === identifyToken) $("#btn-identify").disabled = false;
  }
}

function renderSources(result) {
  const block = $("#sources-block");
  const report = result.report || {};
  const sources = report.sources || [];
  const queries = report.search_queries || [];
  const lens = result.lens_url;

  if (!sources.length && !queries.length && !lens) { block.hidden = true; return; }

  $("#source-list").innerHTML = sources.map((source) => {
    let domain = "";
    try { domain = new URL(source.url).hostname.replace(/^www\./, ""); } catch { domain = ""; }
    return `<li>
      <a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title || source.url)}</a>
      ${domain ? `<span class="domain">${escapeHtml(domain)}</span>` : ""}
    </li>`;
  }).join("");

  $("#query-line").textContent = queries.length
    ? `Searches run: ${queries.map((q) => `"${q}"`).join(" · ")}`
    : (sources.length ? "" : "No vision provider configured — open the photo in Google Lens to identify it.");

  $("#lens-link").innerHTML = lens
    ? `<a class="btn ghost small-btn" href="${escapeHtml(lens)}" target="_blank" rel="noopener noreferrer">🔍 Reverse image search</a>`
    : "";
  block.hidden = false;
}

function renderReport(result) {
  const report = result.report;
  $("#report-empty").hidden = true;
  const node = $("#report");
  node.hidden = false;
  const conf = (value) => `<span class="conf-badge conf-${escapeHtml(value)}">${escapeHtml(value)}</span>`;
  node.innerHTML = `${conf(report.overall_confidence)} ${renderMarkdown(result.report_markdown)}`;
}

function renderMatch(result) {
  const block = $("#match-block");
  block.hidden = false;
  const part = result.matched_part;
  const badge = $("#match-badge");
  const body = $("#match-body");

  if (!part) {
    badge.className = "badge warn";
    badge.textContent = "no stock match";
    const candidates = (result.candidates || [])
      .map((c) => `<button class="btn tiny" data-adopt="${c.id}">Use #${c.id} ${escapeHtml(c.part_name)}</button>`)
      .join(" ");
    body.innerHTML = `
      <p class="muted">This part is not in inventory yet. Add it with the numbers from the report, then generate cards.</p>
      ${candidates ? `<p class="small muted">Closest stock records:</p><div class="row gap wrap">${candidates}</div>` : ""}
      <div class="row gap wrap mt">
        <button class="btn primary" id="btn-adopt-report">Add to inventory from this scan</button>
      </div>`;
    $("#btn-adopt-report").addEventListener("click", () => openDrawer(prefillFromReport(result.report), { scanPrefill: true }));
    $$("[data-adopt]", body).forEach((button) =>
      button.addEventListener("click", () => openStudioFor(Number(button.dataset.adopt))));
    return;
  }

  badge.className = `badge ${part.verified ? "ok" : "warn"}`;
  badge.textContent = part.verified ? "verified stock record" : "unverified record";
  const imageUrl = part.image_path
    ? mediaUrl(part.image_path)
    : (result.image_path ? mediaUrl(result.image_path) : "");

  body.innerHTML = `
    <div class="match-card">
      ${imageUrl ? `<img class="match-photo" src="${escapeHtml(imageUrl)}" alt="${escapeHtml(part.part_name)}" />` : '<div class="image-fallback large">No photo added</div>'}
      <div>
        <div class="part-name">${escapeHtml(part.part_name)}</div>
        <div class="kv">
          <div><span>OEM</span><strong class="mono">${escapeHtml(part.oem_part_number || "—")}</strong></div>
          <div><span>Bin</span><strong>${escapeHtml(part.bin_location || "unassigned")}</strong></div>
          <div><span>Qty</span><strong>${part.quantity}</strong></div>
          <div><span>Price</span><strong>${escapeHtml(money(part.unit_price, part.currency))}</strong></div>
        </div>
        <div class="row gap wrap mt">
          <button class="btn primary" id="btn-match-cards">Generate promo cards</button>
          <button class="btn" data-adjust="1" data-id="${part.id}">＋1 stock</button>
          <button class="btn" data-edit="${part.id}">Edit record</button>
          ${part.verified ? "" : `<button class="btn" data-verify="${part.id}">Mark verified</button>`}
        </div>
      </div>
    </div>`;

  $("#btn-match-cards").addEventListener("click", () => openStudioFor(part.id));
  $$("[data-adjust]", body).forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/parts/${button.dataset.id}/adjust`, {
        method: "POST",
        body: JSON.stringify({ part_id: Number(button.dataset.id), delta: 1, reason: "scan_received", note: "received at counter" }),
      });
      toast("Stock updated");
      await loadParts();
      renderMatch(result);
    } catch (error) { toast(error.message, true); }
  }));
  $$("[data-edit]", body).forEach((button) => button.addEventListener("click", () => {
    const record = state.parts.find((p) => p.id === Number(button.dataset.edit));
    if (record) openDrawer(record);
  }));
  $$("[data-verify]", body).forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/parts/${button.dataset.verify}`, {
        method: "PATCH", body: JSON.stringify({ verified: true }),
      });
      toast("Record verified");
      await loadParts();
      renderMatch(result);
    } catch (error) { toast(error.message, true); }
  }));
}

function mediaUrl(path) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  const normalized = String(path).replace(/\\/g, "/");
  if (normalized.startsWith("/media/") || normalized.startsWith("/api/demo-image/")) return normalized;
  const file = normalized.split("/").pop();
  if (normalized.startsWith("assets/seed/") || normalized.includes("/assets/seed/")) {
    return `/api/demo-image/${encodeURIComponent(file.replace(/\.\w+$/, ""))}`;
  }
  if (normalized.startsWith("uploads/") || normalized.includes("/uploads/")) return `/media/uploads/${encodeURIComponent(file)}`;
  if (normalized.startsWith("exports/") || normalized.includes("/exports/")) return `/media/exports/${encodeURIComponent(file)}`;
  return "";
}

function prefillFromReport(report) {
  const fit = (report.fitment && report.fitment[0]) || {};
  return {
    part_name: report.part_name || "",
    oem_part_number: report.oem_part_number || "",
    alternate_numbers: (report.alternate_numbers || []).join(", "),
    superseded_numbers: (report.superseded_numbers || []).join(", "),
    category: report.category || "",
    quantity: 0,
    unit_price: 0,
    fit_make_model: [fit.make, fit.model].filter(Boolean).join(" "),
    fit_engine_years: [fit.engine, fit.years].filter(Boolean).join(" / "),
    technical_notes: (report.technical_notes || []).join("\n"),
    image_path: state.lastScan ? state.lastScan.image_path : "",
    verified: false,
  };
}

/* ── inventory table ──────────────────────────────────────── */
async function loadParts(query = "") {
  try {
    state.parts = await api(`/api/parts?q=${encodeURIComponent(query)}`);
    renderParts();
    populateStudioSelect();
    const stats = await api("/api/stats");
    renderStats(stats);
  } catch (error) {
    toast(error.message, true);
  }
}

/* Inventory rows: one photo per part, evenly weighted columns and one compact
   action group. Icon-only controls keep their titles and aria-labels so the
   table stays usable without relying on hover alone. */
const PHOTO_ICON_ADD = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
const PHOTO_ICON_REPLACE = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const EDIT_ICON = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const CARDS_ICON = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m4 17 5-5 4 4 3-3 4 4"/></svg>';
const PHOTOS_ICON = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="3" width="15" height="14" rx="2"/><path d="M3 7v12a2 2 0 0 0 2 2h12"/><circle cx="11.5" cy="8" r="1.3"/><path d="m7 15 3.5-3 2.5 2.5 2-2 4 3.5"/></svg>';

function formatReceived(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return `<span class="date-day">${escapeHtml(date.toLocaleDateString(undefined, { dateStyle: "medium" }))}</span>`
    + `<span class="date-time">${escapeHtml(date.toLocaleTimeString(undefined, { timeStyle: "short" }))}</span>`;
}

function renderParts() {
  const body = $("#parts-body");
  const parts = state.parts.filter((part) => {
    if (state.stockFilter === "in") return part.quantity > 0;
    if (state.stockFilter === "out") return part.quantity <= 0;
    if (state.stockFilter === "review") return !part.verified;
    return true;
  });
  $("#inventory-count").textContent = parts.length;
  if (state.receivedSort) {
    parts.sort((a, b) => {
      const aDate = a.last_received_at ? Date.parse(a.last_received_at) : null;
      const bDate = b.last_received_at ? Date.parse(b.last_received_at) : null;
      if (aDate === null) return bDate === null ? 0 : 1;
      if (bDate === null) return -1;
      return state.receivedSort * (aDate - bDate);
    });
  }
  $("#parts-empty").hidden = parts.length > 0;
  body.innerHTML = parts.map((part) => {
    const photoUrl = mediaUrl(part.image_path);
    const name = escapeHtml(part.part_name);
    const status = part.quantity <= 0
      ? '<span class="tag out">out</span>'
      : part.quantity <= 3
        ? `<span class="tag low">low · ${part.quantity}</span>`
        : '<span class="tag in">in stock</span>';
    const verified = part.verified ? "" : '<span class="tag unverified">unverified</span>';
    const photoAction = part.image_path ? "Replace" : "Add";
    const photo = photoUrl
      ? `<a class="part-photo-link" href="${escapeHtml(photoUrl)}" target="_blank" rel="noopener" title="View full-size photo"><img class="part-thumb" src="${escapeHtml(photoUrl)}" alt="Photo of ${name}" loading="lazy" /></a>`
      : '<span class="part-placeholder" aria-hidden="true">◇</span>';
    const sub = `${escapeHtml(part.category || "")}${part.fitment[0] ? " · " + escapeHtml([part.fitment[0].make, part.fitment[0].model].filter(Boolean).join(" ")) : ""}`;
    return `<tr data-id="${part.id}">
      <td class="part-column">
        <div class="part-cell">
          <div class="part-photo">
            ${photo}
            <input class="part-photo-input" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" data-photo-input="${part.id}" hidden />
            <button class="part-photo-action" type="button" data-photo="${part.id}" title="${photoAction} photo for ${name}" aria-label="${photoAction} photo for ${name}">${photoAction === "Replace" ? PHOTO_ICON_REPLACE : PHOTO_ICON_ADD}</button>
          </div>
          <div class="part-details">
            <div class="part-name">${name}</div>
            <div class="part-sub">${sub || "&nbsp;"}</div>
          </div>
        </div>
      </td>
      <td class="oem-column"><span class="part-oem mono">${escapeHtml(part.oem_part_number || "—")}</span></td>
      <td class="bin-column">${escapeHtml(part.bin_location || "—")}</td>
      <td class="qty-column num">${part.quantity}</td>
      <td class="date-column" title="Latest recorded positive stock movement">${formatReceived(part.last_received_at)}</td>
      <td class="price-column num">${escapeHtml(money(part.unit_price, part.currency))}</td>
      <td class="status-column"><div class="part-status">${status}${verified}</div></td>
      <td class="actions-column">
        <div class="row-actions">
          <button class="btn tiny icon-action" type="button" data-edit="${part.id}" title="Edit part" aria-label="Edit ${name}">${EDIT_ICON}</button>
          <button class="btn tiny icon-action" type="button" data-card="${part.id}" title="Generate promo cards" aria-label="Promo cards for ${name}">${CARDS_ICON}</button>
          <button class="btn tiny icon-action" type="button" data-images="${part.id}" title="Review photos" aria-label="Review photos for ${name}">${PHOTOS_ICON}</button>
        </div>
      </td>
    </tr>`;
  }).join("");

  $$('[data-photo]', body).forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    body.querySelector(`[data-photo-input="${button.dataset.photo}"]`)?.click();
  }));
  $$('[data-photo-input]', body).forEach((input) => input.addEventListener("change", async (event) => {
    event.stopPropagation();
    const file = input.files?.[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      await api(`/api/parts/${input.dataset.photo}/image`, { method: "POST", body: formData });
      toast("Photo saved to part");
      await loadParts($("#search").value);
    } catch (error) {
      toast(error.message, true);
      input.value = "";
    }
  }));
  $$(".part-photo-link", body).forEach((link) => link.addEventListener("click", (event) => event.stopPropagation()));

  $$("[data-card]", body).forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    openStudioFor(Number(button.dataset.card));
  }));
  $$("[data-edit]", body).forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const record = state.parts.find((p) => p.id === Number(button.dataset.edit));
    if (record) openDrawer(record);
  }));
  $$("[data-images]", body).forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const part = state.parts.find((p) => p.id === Number(button.dataset.images));
    if (part) showImageReview([{ part_id: part.id, part_name: part.part_name, oem_part_number: part.oem_part_number }]);
  }));
  $$("tr[data-id]", body).forEach((row) => row.addEventListener("click", () => {
    const record = state.parts.find((p) => p.id === Number(row.dataset.id));
    if (record) openDrawer(record);
  }));
}

$("#sort-received")?.addEventListener("click", () => {
  state.receivedSort = state.receivedSort === -1 ? 1 : -1;
  $("#sort-received").textContent = state.receivedSort === -1 ? "Date received ↓" : "Date received ↑";
  renderParts();
  drafts?.saveUi({ receivedSort: state.receivedSort });
});

/* ── drawer / form ────────────────────────────────────────── */
/* The part form is mirrored into localStorage as fields change and the
   selected photo into IndexedDB, so a mobile browser that reloads or kills
   this tab after a detour to another app does not destroy unfinished work.
   A draft is only a local convenience: it is never submitted automatically,
   and it is cleared only after the inventory save succeeds or the owner
   explicitly discards it. */
function drawerElement() { return $("#drawer"); }

function openDrawer(prefill = {}, options = {}) {
  const form = $("#part-form");
  const recordId = prefill?.id ? String(prefill.id) : "";
  const drawer = drawerElement();

  // Adopting a finished scan is an explicit new-part intent: it replaces any
  // old new-part draft without a dialog and is itself kept as the new draft.
  // A stored draft is protected even when the drawer is closed: opening a
  // different record must never silently destroy work the owner parked.
  if (!options.skipDraftGuard && !options.scanPrefill && drafts && hasStoredDraft()) {
    const existing = drafts.loadPartDraft();
    const sameRecord = existing && String(existing.id || "") === recordId;
    if (!sameRecord) {
      const described = existing?.id
        ? `the unsaved draft of part #${existing.id}`
        : "your unsaved new-part draft";
      const proceed = window.confirm(
        `${described.charAt(0).toUpperCase()}${described.slice(1)} is kept on this device and would be replaced. Discard it and continue?`
      );
      if (!proceed) return;
      clearPartDraftState();   // also deletes the stored draft and its photo
    }
  }

  if (drawer.hidden === false) suspendDraftCapture = true;
  form.reset();
  clearFileInput(form.elements.part_image);
  state.partDraftFile = null;

  const fill = () => {
    $("#drawer-title").textContent = recordId ? `Part #${recordId}` : "New part";
    const fit = (prefill.fitment && prefill.fitment[0]) || {};
    const values = {
      id: recordId,
      part_name: prefill.part_name || "",
      oem_part_number: prefill.oem_part_number || "",
      alternate_numbers: Array.isArray(prefill.alternate_numbers) ? prefill.alternate_numbers.join(", ") : (prefill.alternate_numbers || ""),
      superseded_numbers: Array.isArray(prefill.superseded_numbers) ? prefill.superseded_numbers.join(", ") : (prefill.superseded_numbers || ""),
      category: prefill.category || "",
      bin_location: prefill.bin_location || "",
      condition: prefill.condition || "New",
      quantity: prefill.quantity ?? 0,
      unit_price: prefill.unit_price ?? 0,
      fit_make_model: prefill.fit_make_model || [fit.make, fit.model].filter(Boolean).join(" "),
      fit_engine_years: prefill.fit_engine_years || [fit.engine, fit.years].filter(Boolean).join(" / "),
      technical_notes: Array.isArray(prefill.technical_notes) ? prefill.technical_notes.join("\n") : (prefill.technical_notes || ""),
      image_path: prefill.image_path || "",
    };
    Object.entries(values).forEach(([key, value]) => {
      const field = form.elements[key];
      if (field) field.value = value;
    });
    form.elements.verified.checked = Boolean(prefill.verified);
  };

  // A stored draft for exactly this record (or a stored new-part draft when
  // starting a new part) wins over the server values: that is unfinished work.
  // A scan prefill is fresher explicit intent: it replaces the stored draft.
  const stored = drafts ? drafts.loadPartDraft() : null;
  const useDraft = Boolean(stored) && !options.scanPrefill
    && String(stored.id || "") === recordId;

  if (useDraft) {
    suspendDraftCapture = true;
    fill();
    applyDraftToForm(stored);
    suspendDraftCapture = false;
    draftSavedState = false;
    drawer.hidden = false;
    restoreDraftPhoto(stored).catch(() => {});
    setDraftStatus(draftRestoredText(stored), true);
    form.elements.part_name.focus();
    return;
  }

  suspendDraftCapture = true;
  fill();
  renderDrawerPhoto(prefill.image_path || "");
  suspendDraftCapture = false;
  draftSavedState = false;
  drawer.hidden = false;
  if (options.scanPrefill) {
    // Keep the adopted scan as the local draft so a reload does not lose it.
    capturePartDraft();
  } else {
    setDraftStatus("", false);
  }
  form.elements.part_name.focus();
}

function closeDrawer() {
  const drawer = drawerElement();
  if (drawer.hidden) return;
  // Closing hides the drawer but keeps the draft: reopening the same record —
  // or reloading the page — restores it. Nothing is silently destroyed. The
  // closed state is remembered so a reload does not force the drawer back open.
  flushPartDraft();
  drawer.hidden = true;
  drafts?.saveUi({ drawerOpen: false });
}

function renderDrawerPhoto(path, localFile = null) {
  const preview = $("#part-photo-preview");
  if (localFile) {
    preview.innerHTML = `<span class="muted small">Selected photo: ${escapeHtml(localFile.name)}</span><br><img class="drawer-photo-preview" src="${URL.createObjectURL(localFile)}" alt="Selected part photo preview" />`;
    preview.hidden = false;
  } else if (path) {
    preview.innerHTML = `<span class="muted small">Current photo</span><br><img class="drawer-photo-preview" src="${escapeHtml(mediaUrl(path))}" alt="Current part photo" />`;
    preview.hidden = false;
  } else {
    preview.innerHTML = "";
    preview.hidden = true;
  }
}

$("#part-form").elements.part_image.addEventListener("change", (event) => {
  const file = event.target.files?.[0] || null;
  const path = $("#part-form").elements.image_path.value;
  state.partDraftFile = file;
  renderDrawerPhoto(path, file);
  // Persist the newly chosen photo immediately (fields-change persistence).
  if (drafts && file && drafts.draftsEnabled() && typeof file.arrayBuffer === "function") {
    drafts.putPhoto("part", file).catch(() => {});
  }
  capturePartDraft();
});

/* ── part-form draft capture / restore ────────────────────── */
const DRAFT_TEXT_FIELDS = [
  "part_name", "oem_part_number", "alternate_numbers", "superseded_numbers",
  "category", "bin_location", "condition", "quantity", "unit_price",
  "fit_make_model", "fit_engine_years", "technical_notes", "image_path",
];

function draftSnapshot() {
  const form = $("#part-form");
  const draft = { id: form.elements.id.value || "" };
  for (const field of DRAFT_TEXT_FIELDS) draft[field] = form.elements[field]?.value ?? "";
  draft.verified = Boolean(form.elements.verified?.checked);
  const file = form.elements.part_image.files?.[0] || state.partDraftFile;
  if (file && typeof file.arrayBuffer === "function") {
    draft.photo = { name: file.name || "photo", type: file.type || "", size: file.size || 0 };
  }
  return draft;
}

/* Is there unfinished work stored on this device? Independent of whether the
   drawer is currently on screen — closing the drawer must not forget it. */
function hasStoredDraft() {
  if (!drafts || draftSavedState) return false;
  const draft = drafts.loadPartDraft();
  return Boolean(draft) && !drafts._internal.draftIsEmpty(draft);
}

/* …and is it on screen right now (i.e. would opening something else replace
   work the owner can currently see)? */
function partDraftDirty() {
  if (drawerElement().hidden) return false;
  return hasStoredDraft();
}

function setDraftStatus(text, showDiscard) {
  const status = $("#drawer-draft-status");
  const discard = $("#btn-discard-draft");
  if (status) {
    status.textContent = text || "";
    status.classList.toggle("draft-restored", Boolean(text));
    status.hidden = !text;
  }
  if (discard) discard.hidden = !showDiscard;
}

function draftRestoredText(draft) {
  const when = draft?.savedAt ? new Date(draft.savedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  return `Draft restored from this device${when ? ` · saved ${when}` : ""} — not yet saved to inventory.`;
}

function capturePartDraft() {
  if (!drafts || suspendDraftCapture || draftSavedState) return;
  if (drawerElement().hidden) return;
  if (!drafts.draftsEnabled()) return;
  const draft = draftSnapshot();
  if (drafts._internal.draftIsEmpty(draft)) return;
  const result = drafts.savePartDraft(draft);
  drafts.saveUi({ drawerOpen: !drawerElement().hidden });
  const photo = draft.photo;
  if (!photo) {
    // A previously stored photo is only dropped when the draft itself no
    // longer references one, never as a side effect of a text edit.
    const stored = drafts.loadPartDraft();
    if (stored?.photo) drafts.removePhoto("part").catch(() => {});
  }
  setDraftStatus(
    (result?.persisted === false
      ? "Draft kept in memory — local storage is full or blocked. Save to inventory soon."
      : "Draft saved locally — not yet saved to inventory.")
    + (photo ? " Photo included." : ""),
    true,
  );
}

function schedulePartDraft() {
  clearTimeout(partDraftTimer);
  partDraftTimer = setTimeout(capturePartDraft, 350);
  flushPartDraftOnHide = true;
}

function flushPartDraft() {
  clearTimeout(partDraftTimer);
  partDraftTimer = null;
  if (flushPartDraftOnHide) capturePartDraft();
  flushPartDraftOnHide = false;
}

function applyDraftToForm(draft) {
  const form = $("#part-form");
  form.elements.id.value = draft.id || "";
  for (const field of DRAFT_TEXT_FIELDS) {
    if (form.elements[field]) form.elements[field].value = draft[field] ?? "";
  }
  form.elements.verified.checked = draft.verified === true;
  $("#drawer-title").textContent = draft.id ? `Part #${draft.id}` : "New part";
}

/* A file input can only be refilled through DataTransfer, and some engines
   expose `files` as read-only (assignment throws in strict mode). Try the
   standard route, then a defined accessor, and report honestly if neither
   works — a recovered photo is never implied when it was not recovered. */
function attachFileToInput(input, file) {
  if (!input || !file) return false;
  try {
    if (typeof window.DataTransfer === "function") {
      const transfer = new window.DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      const attached = input.files?.[0];
      if (attached && (attached === file || attached.name === file.name)) return true;
    }
  } catch { /* read-only files property — fall through */ }
  try {
    const list = { 0: file, length: 1, item: (index) => (index === 0 ? file : null) };
    Object.defineProperty(input, "files", { value: list, configurable: true, writable: true });
    // Mark it: form.reset()/value="" cannot clear a property we defined, so a
    // later openDrawer must remove it explicitly or a stale photo would follow
    // the owner into the next record.
    input.dataset.syntheticFiles = "1";
    return Boolean(input.files?.[0]);
  } catch { return false; }
}

function clearFileInput(input) {
  if (!input) return;
  if (input.dataset?.syntheticFiles === "1") {
    try { delete input.files; } catch { /* keep going */ }
    delete input.dataset.syntheticFiles;
  }
  try { input.value = ""; } catch { /* some engines reject this; harmless */ }
}

async function restoreDraftPhoto(draft) {
  const form = $("#part-form");
  const marker = draft.photo;
  if (marker) {
    const file = await drafts.getPhoto("part");
    if (file) {
      state.partDraftFile = file;
      const attached = attachFileToInput(form.elements.part_image, file);
      renderDrawerPhoto(form.elements.image_path.value, file);
      if (!attached) {
        // Honest fallback: the photo is in memory and savePart() will upload
        // it, but the input could not be refilled programmatically.
        setDraftStatus(`${draftRestoredText(draft)} Photo recovered in memory — the file field could not be refilled, so re-pick it if the save reports a missing photo.`, true);
      }
      return;
    }
    state.partDraftFile = null;
    renderDrawerPhoto(form.elements.image_path.value || "");
    setDraftStatus(`${draftRestoredText(draft)} The stored photo could not be recovered — re-select it before saving.`, true);
    return;
  }
  state.partDraftFile = null;
  renderDrawerPhoto(form.elements.image_path.value || "");
}

async function restorePartDraft() {
  if (!drafts) return;
  const ui = drafts.loadUi() || {};
  const draft = drafts.loadPartDraft();
  if (!draft || !ui.drawerOpen) return;
  draftSavedState = false;
  openDrawer({ id: draft.id || "" }, { skipDraftGuard: true });
  if (draft.id) {
    // The record may have been deleted while the tab was in the background.
    // Check it still exists rather than offering to PATCH a missing id.
    try {
      await api(`/api/parts/${encodeURIComponent(draft.id)}`);
    } catch (error) {
      if (/not found|404/i.test(error.message || "")) {
        const form = $("#part-form");
        suspendDraftCapture = true;
        form.elements.id.value = "";
        suspendDraftCapture = false;
        $("#drawer-title").textContent = "New part";
        drafts.savePartDraft(draftSnapshot());
        setDraftStatus(`${draftRestoredText(draft)} Record #${draft.id} no longer exists — saving now creates a new part.`, true);
      }
      // Offline or other errors: keep the id; a failing save shows the reason.
    }
  }
}

function discardPartDraft() {
  const proceed = window.confirm("Discard this local draft? The entered values and recovered photo will be deleted from this device. The inventory record itself is not touched.");
  if (!proceed) return;
  clearPartDraftState();
  setDraftStatus("", false);
  toast("Local draft discarded");
}

function clearPartDraftState() {
  clearTimeout(partDraftTimer);
  partDraftTimer = null;
  flushPartDraftOnHide = false;
  suspendDraftCapture = true;
  const form = $("#part-form");
  form.reset();
  clearFileInput(form.elements.part_image);
  form.elements.id.value = "";
  state.partDraftFile = null;
  renderDrawerPhoto("");
  suspendDraftCapture = false;
  draftSavedState = false;
  if (drafts) drafts.clearPartDraft().catch(() => {});
}

const splitList = (value) => String(value || "").split(",").map((s) => s.trim()).filter(Boolean);

async function savePart(event) {
  event.preventDefault();
  const form = event.target;
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton?.disabled) return;
  if (submitButton) { submitButton.disabled = true; submitButton.textContent = "Saving…"; }
  const get = (name) => form.elements[name]?.value ?? "";
  const id = get("id");
  const makeModel = get("fit_make_model").trim();
  const engineYears = get("fit_engine_years").trim();
  // Sent as free text: the server parses it with the same fitment parser the
  // CSV importer uses (two-word makes, open-ended years, fuel words).
  const fitmentText = [makeModel, engineYears].filter(Boolean).join(" ");

  const payload = {
    part_name: get("part_name").trim(),
    oem_part_number: get("oem_part_number").trim(),
    alternate_numbers: splitList(get("alternate_numbers")),
    superseded_numbers: splitList(get("superseded_numbers")),
    fitment: [],
    fitment_text: fitmentText,
    condition: get("condition") || "New",
    bin_location: get("bin_location").trim(),
    quantity: Number(get("quantity") || 0),
    unit_price: Number(get("unit_price") || 0),
    currency: state.config?.currency || "KES",
    category: get("category").trim(),
    technical_notes: get("technical_notes").split("\n").map((s) => s.trim()).filter(Boolean),
    image_path: get("image_path").trim(),
    verified: form.elements.verified.checked,
  };

  try {
    let saved;
    if (id) {
      saved = await api(`/api/parts/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      saved = await api("/api/parts", { method: "POST", body: JSON.stringify(payload) });
      // Record the new id immediately: if the photo upload below fails, the
      // local draft keeps this id, so a retry after reload PATCHes the record
      // it just created instead of inserting a duplicate.
      suspendDraftCapture = true;
      form.elements.id.value = saved.id;
      $("#drawer-title").textContent = `Part #${saved.id}`;
      suspendDraftCapture = false;
      capturePartDraft();
    }
    // The photo comes from the file input, or from a draft recovered out of
    // IndexedDB when the input could not be refilled after a reload.
    const photo = form.elements.part_image.files?.[0] || state.partDraftFile;
    if (photo) {
      const imageForm = new FormData();
      imageForm.append("file", photo);
      saved = await api(`/api/parts/${saved.id}/image`, { method: "POST", body: imageForm });
    }
    // The record (and its photo) are on the server now: the local draft has
    // done its job and is cleared. Failed saves keep it for the retry.
    clearTimeout(partDraftTimer);
    partDraftTimer = null;
    flushPartDraftOnHide = false;
    draftSavedState = true;
    state.partDraftFile = null;
    if (drafts) {
      drafts.clearPartDraft().catch(() => {});
      drafts.saveUi({ drawerOpen: false });
    }
    setDraftStatus("", false);
    closeDrawer();
    await loadParts($("#search").value);
    let successMessage = id ? "Part updated" : `Added part #${saved.id}`;
    if (id && !state.parts.some((part) => part.id === Number(id))) {
      // Editing a name can make a row stop matching the active old-name search.
      // Clear that filter so the saved record remains visible after the update.
      $("#search").value = "";
      await loadParts("");
      successMessage = "Part updated — search cleared to show the renamed record";
    }
    toast(successMessage);
    populateStudioSelect();
  } catch (error) {
    // Keep the draft: the save did not succeed, so the work must survive a
    // reload or a switch away from this tab.
    flushPartDraft();
    if (drafts && partDraftDirty()) {
      setDraftStatus("Save failed — your draft is kept on this device and was not saved to inventory.", true);
    }
    toast(error.message, true);
  } finally {
    if (submitButton) { submitButton.disabled = false; submitButton.textContent = "Save part"; }
  }
}

/* ── studio ───────────────────────────────────────────────── */
function populateStudioSelect() {
  const select = $("#studio-part");
  const previous = select.value;
  select.innerHTML = state.parts.map((part) =>
    `<option value="${part.id}">${escapeHtml(part.part_name)} · ${escapeHtml(part.oem_part_number || "no OEM")}</option>`
  ).join("");
  if (state.parts.some((part) => String(part.id) === previous)) select.value = previous;
}

function setStudioPart(part) {
  if (!state.parts.some((item) => item.id === part.id)) state.parts.unshift(part);
  const select = $("#studio-part");
  if (![...select.options].some((option) => option.value === String(part.id))) {
    const option = document.createElement("option");
    option.value = String(part.id);
    option.textContent = `${part.part_name} · ${part.oem_part_number || "no OEM"}`;
    select.append(option);
  }
  select.value = String(part.id);
  $("#studio-selected-part").textContent = `${part.part_name} · ${part.oem_part_number || "no OEM number"}`;
  $("#studio-search-results").hidden = true;
  $("#studio-search-results").innerHTML = "";
}

async function searchStudioParts() {
  const query = $("#studio-search").value.trim();
  const results = $("#studio-search-results");
  if (!query) {
    toast("Enter a part name or number to search", true);
    $("#studio-search").focus();
    return;
  }
  const button = $("#btn-search-part");
  button.disabled = true;
  button.textContent = "Searching…";
  try {
    const matches = await api(`/api/parts?q=${encodeURIComponent(query)}&limit=1000`);
    results.hidden = false;
    results.innerHTML = matches.length
      ? matches.map((part) => `<button class="btn ghost studio-search-result" type="button" role="option" data-studio-result="${part.id}"><strong>${escapeHtml(part.part_name)}</strong><small>${escapeHtml(part.oem_part_number || "No OEM number")} · ${part.quantity} in stock</small></button>`).join("")
      : '<span class="muted small">No matching parts found.</span>';
    $$('[data-studio-result]', results).forEach((resultButton) => resultButton.addEventListener("click", () => {
      const part = matches.find((item) => item.id === Number(resultButton.dataset.studioResult));
      if (part) setStudioPart(part);
    }));
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Search";
  }
}

function openStudioFor(partId) {
  switchView("studio");
  populateStudioSelect();
  const part = state.parts.find((item) => item.id === partId);
  if (part) setStudioPart(part);
  generateCards();
}

async function generateCards() {
  const partId = $("#studio-part").value;
  if (!partId) { toast("Pick a part first", true); return; }
  const formats = $$("#format-toggles input:checked").map((input) => input.value);
  if (!formats.length) { toast("Select at least one format", true); return; }

  const status = $("#studio-status");
  status.className = "status busy";
  status.textContent = "Rendering cards…";
  const generateButton = $("#btn-generate");
  generateButton.disabled = true;

  try {
    // Always load the current database record before generating. The inventory
    // list/search result can be stale after verification or an edit.
    const part = await api(`/api/parts/${encodeURIComponent(partId)}`);
    setStudioPart(part);
    const result = await api("/api/cards", {
      method: "POST",
      body: JSON.stringify({
        part_id: part.id,
        formats,
        headline: $("#studio-headline").value.trim(),
      }),
    });
    const output = $("#card-output");
    renderSocialLinks($("#studio-social-links"), state.config?.social_links);
    $("#caption-block").hidden = false;
    $("#caption-text").textContent = result.caption;
    output.innerHTML = result.cards.map((card) => `
      <div class="card-item">
        <a href="${escapeHtml(card.url)}" target="_blank" rel="noopener">
          <img src="${escapeHtml(card.url)}" alt="${escapeHtml(result.part.part_name)} ${escapeHtml(card.format)} card" loading="lazy" />
        </a>
        <div class="card-meta">
          <span>${escapeHtml(card.format)} · ${card.width}×${card.height}</span>
          <a class="btn tiny" href="/api/cards/download/${escapeHtml(card.filename)}" download>Download</a>
        </div>
      </div>`).join("");

    $("#caption-block").hidden = false;
    $("#caption-text").textContent = result.caption;
    status.className = "status";
    status.textContent = `${result.cards.length} card(s) ready — tap a card to open full size, then share to WhatsApp.`;
  } catch (error) {
    status.className = "status error";
    status.textContent = error.message;
    toast(error.message, true);
  } finally {
    generateButton.disabled = false;
  }
}

/* ── stock file import ────────────────────────────────────── */
let pendingStockFile = null;
let pendingUpdateExisting = false;
let stockImportInProgress = false;

async function sendStockFile(file, dryRun, updateExisting = false) {
  if (stockImportInProgress) return;
  if (!/\.(csv|zip|xlsx|pdf)$/i.test(file.name)) { toast("Choose a CSV, ZIP, Excel or PDF stock file", true); return; }
  switchView("inventory");
  stockImportInProgress = true;
  pendingStockFile = null;
  $("#import-actions").innerHTML = "";
  const block = $("#import-block");
  const summary = $("#import-summary");
  block.hidden = false;
  summary.innerHTML = `<p class="status busy">${dryRun ? "Checking" : "Importing"} ${escapeHtml(file.name)}…</p>`;

  const form = new FormData();
  form.append("file", file);

  try {
    const query = `?dry_run=${dryRun}&update_existing=${updateExisting}&review_images=true`;
    const result = await api(`/api/inventory/import${query}`, { method: "POST", body: form });
    renderImportSummary(result, file, dryRun, updateExisting);
    if (!dryRun) {
      pendingStockFile = null;
      await loadParts($("#search").value);
      populateStudioSelect();
      toast(`Imported ${result.created} part(s)`);
      showImageReview(result.results.filter((r) => r.part_id));
    } else {
      pendingStockFile = file;
      pendingUpdateExisting = updateExisting;
    }
  } catch (error) {
    summary.innerHTML = `<p class="status error">${escapeHtml(error.message)}</p>`;
    const applyButton = $("#btn-apply-import");
    if (applyButton) { applyButton.disabled = false; applyButton.textContent = "Apply import"; }
  } finally {
    stockImportInProgress = false;
  }
}

function renderImportSummary(result, file, dryRun, mergeMatches) {
  const summary = $("#import-summary");
  const actions = $("#import-actions");
  const aliasRows = (result.results || []).filter((row) =>
    row.status === "skipped" && row.message.includes("alternate/superseded number")
  );
  const stat = (label, value, cls = "") =>
    `<div class="${cls}"><strong>${value}</strong>${label}</div>`;

  summary.innerHTML = `
    <p class="muted small">${escapeHtml(file.name)} · ${result.rows_total} rows read${dryRun ? " · dry run, nothing written yet. Photo paths are deferred for approval" : ""}</p>
    ${result.bundle ? `<p class="status">${result.bundle.images_matched} images matched by part number · ${result.bundle.images_missing} rows without an image · ${result.bundle.unused_images.length} unused images. Photos require individual approval.${result.bundle.test_only ? " TEST PACKAGE: these images are labelled placeholders, not part photos." : ""}</p>` : ""}
    <div class="import-stats">
      ${stat("new parts", result.created, "good")}
      ${stat(mergeMatches ? "matched · qty added" : "updated", result.updated)}
      ${stat("already in stock", result.skipped)}
      ${stat("problems", result.errors, result.errors ? "bad" : "")}
    </div>
    ${mergeMatches ? '<p class="import-cols">Matching OEM or alternate numbers use the existing part record; uploaded quantities are added to current stock. The Excel file is not changed.</p>' : ""}
    ${result.images_found ? `<p class="import-cols">${dryRun ? `${result.images_found} embedded photo(s) found` : `${result.images_pending || 0} photo(s) awaiting individual approval`}</p>` : ""}
    ${aliasRows.length ? `<p class="import-cols">${aliasRows.length} row(s) whose OEM number is listed as an alternate/superseded number were kept as references and not added as separate parts: ${aliasRows.slice(0, 8).map((row) => `row ${row.row} · ${escapeHtml(row.oem_part_number)}`).join(", ")}${aliasRows.length > 8 ? ", …" : ""}</p>` : ""}
    ${result.problems.length ? `<ul class="import-problems">${result.problems.map((p) => `<li>row ${p.row}: ${escapeHtml(p.message)}</li>`).join("")}</ul>` : ""}
    <p class="import-cols">Columns mapped: ${Object.entries(result.columns_mapped).map(([from, to]) => `${escapeHtml(from)} → ${escapeHtml(to)}`).join(", ") || "none"}${result.ignored_columns.length ? ` · ignored: ${result.ignored_columns.map(escapeHtml).join(", ")}` : ""}</p>`;

  summary.insertAdjacentHTML("beforeend", `<div class="table-wrap"><table class="table"><thead><tr><th>Row</th><th>Part / OEM</th><th>Result</th></tr></thead><tbody>${result.results.map((r) => `<tr><td>${r.row}</td><td>${escapeHtml(r.part_name)}<div class="part-sub">${escapeHtml(r.oem_part_number)}</div>${r.image_filename ? `<div class="part-sub">Photo: ${escapeHtml(r.image_filename)}</div>` : ""}</td><td>${escapeHtml(r.status)}${r.message ? ` · ${escapeHtml(r.message)}` : ""}</td></tr>`).join("")}</tbody></table></div>${result.rows_total > result.results.length ? '<p class="muted small">Showing the first 500 results. Use Photos in inventory to review other rows.</p>' : ""}`);

  actions.innerHTML = dryRun
    ? `<button class="btn primary small-btn" id="btn-apply-import" ${result.created + result.updated === 0 && !(result.bundle?.images_matched && result.skipped) ? "disabled" : ""}>Apply import</button>
       <button class="btn ghost small-btn" id="btn-cancel-import">Cancel</button>`
    : `<button class="btn ghost small-btn" id="btn-cancel-import">Dismiss</button>`;

  if (dryRun) {
    $("#btn-apply-import").addEventListener("click", (event) => {
      const button = event.currentTarget;
      if (stockImportInProgress) return;
      button.disabled = true;
      button.textContent = "Importing…";
      if (pendingStockFile) sendStockFile(pendingStockFile, false, pendingUpdateExisting);
    });
  }
  $("#btn-cancel-import").addEventListener("click", () => {
    pendingStockFile = null;
    $("#import-block").hidden = true;
    $("#csv-input").value = "";
  });
}

/* Image choices never write anything until that row's Approve button is clicked. */
function showImageReview(rows) {
  const block = $("#image-review");
  const unique = [...new Map(rows.map((r) => [r.part_id, r])).values()];
  block.hidden = unique.length === 0;
  const container = $("#image-review-rows");
  container.innerHTML = unique.map((r) => `<div class="review-row" data-review="${r.part_id}">
    <strong>${escapeHtml(r.part_name)}</strong> <span class="muted small">${escapeHtml(r.oem_part_number || "No OEM")}</span>
    <div class="row gap wrap mt"><button type="button" class="btn small-btn" data-suggest="${r.part_id}">Find local candidates</button>
    <button type="button" class="btn ghost small-btn" data-skip>Skip / keep current photo</button></div>
    <div class="review-candidates"></div><p class="status" role="status"></p></div>`).join("");
  $$("[data-skip]", container).forEach((button) => button.addEventListener("click", () => {
    const row = button.closest(".review-row");
    $(".review-candidates", row).innerHTML = "";
    $(".status", row).textContent = "Skipped. Current photo unchanged.";
  }));
  $$("[data-suggest]", container).forEach((button) => button.addEventListener("click", async () => {
    const row = button.closest(".review-row");
    const output = $(".review-candidates", row);
    const status = $(".status", row);
    button.disabled = true;
    status.textContent = "Looking in the local catalogue…";
    try {
      const result = await api(`/api/parts/${button.dataset.suggest}/image-candidates`);
      output.innerHTML = result.candidates.map((c) => `<div class="review-candidate">
        <img src="${escapeHtml(c.image_url)}" alt="${escapeHtml(c.part_name)} candidate" loading="lazy" />
        <p>${escapeHtml(c.part_name)}<br />${escapeHtml(c.oem_part_number || "No OEM")}<br />${escapeHtml(c.reason)}</p>
        <button class="btn primary small-btn" type="button" data-approve="${escapeHtml(c.candidate_id)}">Approve this photo</button></div>`).join("");
      status.textContent = result.candidates.length ? "Suggestions only. Approve a photo or skip this row." : "No local candidates. Keep this row without a photo, or add a photo via Edit.";
      $$("[data-approve]", output).forEach((approve) => approve.addEventListener("click", async () => {
        const controls = $$("button", row);
        controls.forEach((control) => { control.disabled = true; });
        try {
          await api(`/api/parts/${button.dataset.suggest}/image-approval`, {
            method: "POST", body: JSON.stringify({ candidate_id: approve.dataset.approve }),
          });
          output.innerHTML = "";
          status.textContent = "Photo approved and saved. OEM verification unchanged.";
          await loadParts($("#search").value);
        } catch (error) { status.textContent = error.message; }
        finally { controls.forEach((control) => { control.disabled = false; }); }
      }));
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  }));
}

function chooseStockFile(file) {
  if (file) sendStockFile(file, true, $("#import-update-existing").checked);
}

function openImportPanel() {
  const block = $("#import-block");
  block.hidden = false;
  block.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
}

/* ── navigation ───────────────────────────────────────────── */
async function loadVerificationSummary() {
  const status = $("#report-summary-status");
  status.textContent = "Loading verification totals…";
  try {
    const stats = await api("/api/stats");
    $("#report-verified-count").textContent = Number(stats.verified_parts || 0).toLocaleString();
    $("#report-unverified-count").textContent = Number(stats.unverified_parts || 0).toLocaleString();
    status.textContent = `${Number(stats.parts || 0).toLocaleString()} inventory records`;
  } catch (error) {
    status.textContent = `Could not load report totals: ${error.message}`;
  }
}

function switchView(name) {
  // Persist where the owner was: after a mobile reload or tab-kill the same
  // tab is shown again instead of falling back to Identify/home.
  if (drafts && state.view && state.view !== name) {
    drafts.saveUi({ scroll: { ...scrollMemory(), [state.view]: currentScrollY() } });
  }
  state.view = name;
  $$(".tab").forEach((tab) => {
    const selected = tab.dataset.view === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  const descriptions = {
    identify: ["Identify a part", "Start with a photo. Find the details that matter.", "PHOTO → REVIEW → STOCK"],
    inventory: ["Your inventory", "Every OEM number, shelf and stock level. In one place.", "FIND → VERIFY → ORGANISE"],
    reports: ["Reports", "Verification and inventory exports for your shop.", "REVIEW → EXPORT"],
    settings: ["Shop settings", "Your business details and public social profiles.", "EDIT → SAVE"],
    studio: ["Promo studio", "Turn verified stock into something worth sharing.", "CHOOSE → CREATE → SHARE"],
  };
  const [title, description, workflow] = descriptions[name] || descriptions.identify;
  $("#workspace-title").textContent = title;
  $("#workspace-description").textContent = description;
  $("#workspace-step").textContent = workflow;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  if (name === "inventory") loadParts($("#search").value);
  if (name === "reports") loadVerificationSummary();
  if (name === "studio") populateStudioSelect();
  if (drafts) drafts.saveUi({ view: name });
  const target = drafts?.loadUi()?.scroll?.[name];
  window.scrollTo({ top: Number(target) || 0, behavior: target ? "auto" : "smooth" });
}

/* ── local draft restore / persistence wiring ─────────────── */
function currentScrollY() {
  const y = Number(window.scrollY || 0);
  return Number.isFinite(y) && y > 0 ? Math.min(y, 100000) : 0;
}

function scrollMemory() {
  return (drafts && drafts.loadUi()?.scroll) || {};
}

function persistUiState() {
  if (!drafts) return;
  drafts.saveUi({
    view: state.view,
    search: $("#search")?.value || "",
    stockFilter: state.stockFilter,
    receivedSort: state.receivedSort,
    drawerOpen: drawerElement().hidden === false && hasStoredDraft(),
    scroll: { ...scrollMemory(), [state.view]: currentScrollY() },
  });
}

function restoreUiDraft() {
  if (!drafts) return;
  const ui = drafts.loadUi();
  if (!ui) return;
  if ($("#search")) $("#search").value = ui.search || "";
  if (ui.stockFilter) {
    state.stockFilter = ui.stockFilter;
    $$("[data-stock-filter]").forEach((chip) => {
      const selected = chip.dataset.stockFilter === ui.stockFilter;
      chip.classList.toggle("active", selected);
      chip.setAttribute("aria-pressed", String(selected));
    });
  }
  if (ui.receivedSort) {
    state.receivedSort = ui.receivedSort;
    const button = $("#sort-received");
    if (button) button.textContent = ui.receivedSort === -1 ? "Date received ↓" : "Date received ↑";
  }
  syncDraftPrefUi();
  if (ui.view && $(`#tab-${ui.view}`)) switchView(ui.view);
}

function restoreScanDraft() {
  if (!drafts || !drafts.draftsEnabled()) return;
  const stored = drafts.loadScan();
  if (!stored || state.lastScan) return;
  const result = stored.result;
  state.lastScan = result;
  renderReport(result);
  renderSources(result);
  renderMatch(result);
  const status = $("#identify-status");
  const when = stored.savedAt ? new Date(stored.savedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  status.className = "status";
  status.textContent = `Identification result restored from this device${when ? ` (saved ${when})` : ""} — it was not re-run.`;
  $("#btn-identify").disabled = false;
  // Recover the captured photo from IndexedDB when it is still there.
  drafts.getPhoto("identify").then((file) => {
    if (!file) {
      status.textContent += " The photo itself was not recovered — re-add it to run a new identification.";
      return;
    }
    // A restore must never trigger auto-detect: no interrupted or completed
    // identification request is ever re-run automatically.
    state.selectedFile = file;
    const preview = $("#preview");
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
    $("#dropzone-idle").hidden = true;
  }).catch(() => {});
}

function wireDraftPersistence() {
  if (!drafts) return;
  const form = $("#part-form");
  form.addEventListener("input", schedulePartDraft, { capture: true });
  form.addEventListener("change", schedulePartDraft, { capture: true });

  $("#btn-discard-draft")?.addEventListener("click", discardPartDraft);

  const keep = $("#keep-drafts");
  if (keep) {
    syncDraftPrefUi();
    keep.addEventListener("change", () => {
      drafts.setDraftsEnabled(keep.checked);
      if (!keep.checked) {
        clearPartDraftState();
        setDraftStatus("", false);
        toast("Local drafts turned off and deleted from this device");
      } else {
        toast("Local drafts will be kept on this device");
      }
    });
  }

  // Mobile browsers can kill a background tab at any moment: persist on every
  // hide, not just on unload (which often never fires on phones).
  const persistNow = () => {
    flushPartDraft();
    persistUiState();
  };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") persistNow(); });
  window.addEventListener("pagehide", persistNow);
  window.addEventListener("beforeunload", persistNow);
}

function syncDraftPrefUi() {
  const keep = $("#keep-drafts");
  if (keep && drafts) keep.checked = drafts.draftsEnabled();
}

function wireUi() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
  const nav = $(".nav-items");
  const setOrientation = () => nav.setAttribute("aria-orientation", window.innerWidth <= 680 ? "horizontal" : "vertical");
  setOrientation();
  window.addEventListener("resize", setOrientation);
  nav.addEventListener("keydown", (event) => {
    const tabs = $$(".tab", nav);
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    const vertical = nav.getAttribute("aria-orientation") === "vertical";
    const nextKey = vertical ? "ArrowDown" : "ArrowRight";
    const previousKey = vertical ? "ArrowUp" : "ArrowLeft";
    let next;
    if (event.key === nextKey) next = (index + 1) % tabs.length;
    else if (event.key === previousKey) next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    switchView(tabs[next].dataset.view);
    tabs[next].focus();
  });
  $$("[data-stock-filter]").forEach((button) => button.addEventListener("click", () => {
    state.stockFilter = button.dataset.stockFilter;
    $$("[data-stock-filter]").forEach((filter) => {
      const selected = filter === button;
      filter.classList.toggle("active", selected);
      filter.setAttribute("aria-pressed", String(selected));
    });
    renderParts();
    drafts?.saveUi({ stockFilter: state.stockFilter });
  }));
  $("#btn-signout").addEventListener("click", async () => {
    // Explicit logout wipes this account's local drafts and photos first — the
    // wipe happens even if the sign-out request itself then fails.
    if (drafts) {
      try { await drafts.clearAccount(); } catch { /* best effort */ }
    }
    try {
      await api("/api/auth/signout", { method: "POST" });
      window.location.assign("/login");
    } catch (error) { toast(error.message, true); }
  });
  $("#shop-settings-form").addEventListener("submit", saveShopSettings);
  $("#part-form").addEventListener("submit", savePart);
  $$("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
  $("#btn-new-part").addEventListener("click", () => openDrawer({}));
  $("#csv-dropzone").addEventListener("click", () => $("#csv-input").click());
  const csvZone = $("#csv-dropzone");
  ["dragenter", "dragover"].forEach((name) => csvZone.addEventListener(name, (event) => {
    event.preventDefault();
    csvZone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((name) => csvZone.addEventListener(name, (event) => {
    event.preventDefault();
    csvZone.classList.remove("dragover");
  }));
  csvZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files[0];
    if (file) chooseStockFile(file);
  });
  $("#close-image-review").addEventListener("click", () => { $("#image-review").hidden = true; });
  $("#btn-import").addEventListener("click", openImportPanel);
  $("#btn-close-import").addEventListener("click", () => { $("#import-block").hidden = true; });
  $("#btn-pick-stock-file").addEventListener("click", (event) => {
    event.stopPropagation();
    $("#csv-input").click();
  });
  $("#import-update-existing").addEventListener("change", (event) => {
    pendingUpdateExisting = event.target.checked;
  });
  $("#csv-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    chooseStockFile(file);
    event.target.value = "";
  });
  const stockDropzone = $("#stock-dropzone");
  stockDropzone.addEventListener("click", () => $("#csv-input").click());
  stockDropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("#csv-input").click(); }
  });
  ["dragenter", "dragover"].forEach((type) => stockDropzone.addEventListener(type, (event) => {
    event.preventDefault(); stockDropzone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((type) => stockDropzone.addEventListener(type, (event) => {
    event.preventDefault(); stockDropzone.classList.remove("dragover");
  }));
  stockDropzone.addEventListener("drop", (event) => {
    const files = event.dataTransfer.files;
    if (files.length > 1) { toast("Choose one stock file at a time", true); return; }
    chooseStockFile(files[0]);
  });
  $("#studio-part").addEventListener("change", () => {
    const part = state.parts.find((item) => String(item.id) === $("#studio-part").value);
    if (part) setStudioPart(part);
  });
  $("#btn-generate").addEventListener("click", generateCards);
  $("#btn-search-part").addEventListener("click", searchStudioParts);
  $("#studio-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); searchStudioParts(); }
  });
  $("#btn-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#caption-text").textContent);
      toast("Caption copied");
    } catch {
      toast("Copy failed — select the text manually", true);
    }
  });
  let timer;
  $("#search").addEventListener("input", (event) => {
    clearTimeout(timer);
    drafts?.saveUi({ search: event.target.value });
    timer = setTimeout(() => loadParts(event.target.value), 220);
  });
}

wireDropzone();
wireUi();
wireDraftPersistence();
boot();
