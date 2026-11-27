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
  account: null,
  draft: null,
  draftPhoto: null,
  photoMissing: false,
  photoStorageFailed: false,
  photoSaving: false,
};

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

/* Phone cameras produce 4–12 MB photos; uploading those over a home
   Tailscale link is what made saving feel slow. Downscale once in the
   browser to the same 1600px edge the server stores, as JPEG — usually
   200–500 KB. Anything we cannot decode (e.g. some HEIC files) falls
   back to the original and the server normalises it as before. */
const UPLOAD_MAX_EDGE = 1600;
const UPLOAD_JPEG_QUALITY = 0.82;
const UPLOAD_SKIP_BELOW_BYTES = 900 * 1024; // already small enough to send as-is

async function prepareImageForUpload(file) {
  if (!file || !/^image\//i.test(file.type || "")) return file;
  try {
    const bitmap = await createImageBitmap(file);
    try {
      const longest = Math.max(bitmap.width, bitmap.height);
      const scale = Math.min(1, UPLOAD_MAX_EDGE / longest);
      if (scale >= 1 && file.size <= UPLOAD_SKIP_BELOW_BYTES) return file;
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d");
      if (!ctx) return file;
      ctx.drawImage(bitmap, 0, 0, width, height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", UPLOAD_JPEG_QUALITY));
      if (!blob) return file;
      if (blob.size >= file.size) return file; // re-encode gained us nothing
      const base = (file.name || "part-photo").replace(/\.[^.]+$/, "");
      return new File([blob], `${base}.jpg`, { type: "image/jpeg", lastModified: Date.now() });
    } finally {
      bitmap.close?.();
    }
  } catch {
    return file; // undecodable here — send the original, server still normalises
  }
}

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
    window.location.assign("/login");
    throw new Error("Please sign in to your local account.");
  }
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : `request failed (${response.status})`;
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
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
    const [config, health, stats, session] = await Promise.all([
      api("/api/config"), api("/api/health"), api("/api/stats"), api("/api/auth/session"),
    ]);
    $("#auth-email").textContent = session.email || "";
    state.account = session.authenticated && session.email ? session.email.toLowerCase() : null;
    state.config = config;
    populateShopSettings(config);
    renderSocialLinks($("#studio-social-links"), config.social_links);
    $("#shop-name").textContent = config.shop_name;
    document.title = `${config.shop_name} — Parts workspace`;
    $("#shop-tagline").textContent = config.tagline;
    $("#max-mb").textContent = config.max_upload_mb;
    renderProviderBadge(health);
    renderStats(stats);
    await loadParts();
    await restoreWorkspace();
    await loadDemoImages();
    populateStudioSelect();
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

function selectFile(file) {
  state.selectedFile = file;
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
  status.className = "status busy";
  status.textContent = "Checking local photos first, then identifying if needed…";
  $("#btn-identify").disabled = true;

  const upload = await prepareImageForUpload(state.selectedFile);
  if (token !== identifyToken) return; // a newer upload superseded this one while compressing
  const form = new FormData();
  form.append("file", upload, upload.name || "photo.jpg");

  try {
    const result = await api("/api/identify", { method: "POST", body: form });
    if (token !== identifyToken) return; // a newer upload superseded this one
    state.lastScan = result;
    renderReport(result);
    renderSources(result);
    renderMatch(result);
    status.className = "status";
    const conf = result.report.overall_confidence;
    const via = result.report.grounded ? `${result.report.provider} + web search` : result.report.provider;
    status.textContent = result.report.provider === "local-inventory"
      ? "Local photo suggestion found — confirm the part before updating stock."
      : `Done — ${conf} confidence via ${via}.`;
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
  const photoCandidates = result.photo_candidates || [];
  const part = result.selected_part || result.matched_part || photoCandidates[0]?.part;
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
    $("#btn-adopt-report").addEventListener("click", () => openDrawer(prefillFromReport(result.report)));
    $$("[data-adopt]", body).forEach((button) => button.addEventListener("click", () => {
      result.selected_part = result.candidates.find((candidate) => candidate.id === Number(button.dataset.adopt));
      renderMatch(result);
    }));
    return;
  }

  badge.className = "badge warn";
  badge.textContent = "possible match — confirm part";
  const imageUrl = photoCandidates.find((c) => c.part.id === part.id)?.reference_url
    || (part.image_path ? mediaUrl(part.image_path) : "");

  const otherCandidates = [result.matched_part, ...(result.candidates || [])]
    .filter((candidate, index, all) => candidate && !photoCandidates.some((c) => c.part.id === candidate.id) && all.findIndex((c) => c?.id === candidate.id) === index);
  body.innerHTML = `
    <p class="small muted">Photo and identification matches are suggestions, not proof. Check the physical part, OEM and fitment before adding stock.</p>
    ${photoCandidates.length ? `<div class="row gap wrap warehouse-results">${photoCandidates.map((c) => `<button type="button" class="btn small-btn" data-photo-candidate="${c.part.id}">Photo: ${escapeHtml(c.part.part_name)} · ${escapeHtml(c.part.oem_part_number || "no OEM")}</button>`).join("")}</div>` : ""}
    ${otherCandidates.length ? `<div class="row gap wrap warehouse-results">${otherCandidates.map((c) => `<button type="button" class="btn small-btn" data-report-candidate="${c.id}">Report: ${escapeHtml(c.part_name)} · ${escapeHtml(c.oem_part_number || "no OEM")}</button>`).join("")}</div>` : ""}
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
          <label class="field"><span>Quantity received</span><input id="scan-receive-qty" class="input" type="number" min="1" step="1" value="1" /></label>
          <button class="btn" data-adjust="1" data-id="${part.id}">Confirm match &amp; add stock</button>
          <button class="btn" data-edit="${part.id}">Edit record</button>
          ${part.verified ? "" : `<button class="btn" data-verify="${part.id}">Mark verified</button>`}
        </div>
      </div>
    </div>`;

  $$("[data-photo-candidate]", body).forEach((button) => button.addEventListener("click", () => {
    result.selected_part = photoCandidates.find((c) => c.part.id === Number(button.dataset.photoCandidate)).part;
    renderMatch(result);
  }));
  $$("[data-report-candidate]", body).forEach((button) => button.addEventListener("click", () => {
    result.selected_part = otherCandidates.find((c) => c.id === Number(button.dataset.reportCandidate));
    renderMatch(result);
  }));
  $("#btn-match-cards").addEventListener("click", () => openStudioFor(part.id));
  $$("[data-adjust]", body).forEach((button) => button.addEventListener("click", async () => {
    const quantity = Number($("#scan-receive-qty").value);
    if (!Number.isSafeInteger(quantity) || quantity < 1) { toast("Enter a whole quantity of at least 1", true); return; }
    if (!confirm(`Confirm ${part.part_name} (${part.oem_part_number || "no OEM"}) is the same part and add ${quantity} to stock?`)) return;
    button.disabled = true;
    try {
      const updated = await api(`/api/parts/${button.dataset.id}/adjust`, {
        method: "POST",
        body: JSON.stringify({ part_id: Number(button.dataset.id), delta: quantity, reason: "scan_received", note: "photo match confirmed by user" }),
      });
      result.selected_part = updated;
      result.photo_candidates = photoCandidates.map((c) => c.part.id === updated.id ? { ...c, part: updated } : c);
      toast("Stock received");
      await loadParts();
      renderMatch(result);
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  $$("[data-edit]", body).forEach((button) => button.addEventListener("click", () => {
    const record = state.parts.find((p) => p.id === Number(button.dataset.edit));
    if (record) openDrawer(record);
  }));
  $$("[data-verify]", body).forEach((button) => button.addEventListener("click", async () => {
    try {
      const updated = await api(`/api/parts/${button.dataset.verify}`, {
        method: "PATCH", body: JSON.stringify({ verified: true }),
      });
      result.selected_part = updated;
      result.photo_candidates = photoCandidates.map((c) => c.part.id === updated.id ? { ...c, part: updated } : c);
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

/* The parts table shows 46px thumbnails — point it at the small server-side
   thumbnail (a few KB) instead of the 1600px upload. The full image stays
   one tap away via the thumbnail link. */
function thumbUrl(path) {
  const url = mediaUrl(path);
  const match = url.match(/^\/media\/uploads\/(\d{8}-[0-9a-f]{10}\.jpg)$/i);
  return match ? `/api/thumbs/${match[1]}` : url;
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
    // Fetch the list and the stats together — one round trip less on a phone.
    const [parts, stats] = await Promise.all([
      api(`/api/parts?q=${encodeURIComponent(query)}`),
      api("/api/stats").catch(() => null),
    ]);
    state.parts = parts;
    renderParts();
    populateStudioSelect();
    if (stats) renderStats(stats);
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
      ? `<a class="part-photo-link" href="${escapeHtml(photoUrl)}" target="_blank" rel="noopener" title="View full-size photo"><img class="part-thumb" src="${escapeHtml(thumbUrl(part.image_path))}" alt="Photo of ${name}" loading="lazy" decoding="async" /></a>`
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
    input.value = ""; // allow re-picking the same photo; compress before upload
    let upload;
    try {
      upload = await prepareImageForUpload(file);
    } catch {
      upload = file;
    }
    const formData = new FormData();
    formData.append("file", upload, upload.name || "part-photo.jpg");
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
  saveWorkspace();
});

/* Local recovery is scoped to the authenticated account. Text is written
   synchronously on every change; photos are stored separately in IndexedDB. */
const draftFields = ["id", "part_name", "oem_part_number", "alternate_numbers", "superseded_numbers", "category", "bin_location", "condition", "quantity", "unit_price", "fit_make_model", "fit_engine_years", "technical_notes", "image_path"];
const storageKey = (kind) => `verifix:${encodeURIComponent(state.account)}:${kind}:v1`;
function draftMessage(text) { $("#draft-status").textContent = text; }
function saveDraft() {
  if (!state.account || $("#drawer").hidden) return;
  const form = $("#part-form");
  const fields = Object.fromEntries(draftFields.map((name) => [name, form.elements[name].value]));
  fields.verified = form.elements.verified.checked;
  state.draft = { version: 1, fields, photoKey: state.draft?.photoKey || null, photoName: state.draft?.photoName || "photo", uncertainCreate: !!state.draft?.uncertainCreate };
  try {
    localStorage.setItem(storageKey("draft"), JSON.stringify(state.draft));
    draftMessage(state.draft.uncertainCreate ? "Previous creation may have completed. Check inventory before discarding; retry is blocked to prevent duplicates." : state.photoMissing ? "Photo not recovered — reselect it. Draft is not saved to inventory." : state.photoStorageFailed ? "Photo could not be saved locally; reselect it after reload. Draft is not saved to inventory." : state.photoSaving ? "Saving photo locally… Draft is not saved to inventory." : "Draft saved locally — not yet saved to inventory.");
  } catch { draftMessage("Local draft could not be saved (storage unavailable or full). Keep this page open; save to inventory."); }
}
function photoDB() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) { reject(new Error("Photo storage unavailable")); return; }
    const request = indexedDB.open("verifix-draft-photos", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("photos");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("Photo storage blocked"));
  });
}
async function photoOperation(mode, key, value) {
  const db = await photoDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("photos", mode === "get" ? "readonly" : "readwrite");
    const store = tx.objectStore("photos");
    const request = mode === "get" ? store.get(key) : mode === "put" ? store.put(value, key) : store.delete(key);
    tx.oncomplete = () => { db.close(); resolve(request.result); };
    tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
  });
}
function photoStorageKey(key) { return `${state.account}:${key}`; }
const pendingPhotoOperations = new Set();
function trackPhoto(promise) {
  pendingPhotoOperations.add(promise);
  promise.finally(() => pendingPhotoOperations.delete(promise)).catch(() => {});
  return promise;
}
async function storePhoto(key, file) {
  if (!key || !state.account) return;
  const storage = photoStorageKey(key);
  try {
    await photoOperation("put", storage, file);
    // If the draft was discarded or replaced while writing, remove the orphan.
    if (state.draft?.photoKey !== key) await photoOperation("delete", storage);
    else { state.photoSaving = false; saveDraft(); }
  } catch {
    if (state.draft?.photoKey === key) { state.photoSaving = false; state.photoStorageFailed = true; draftMessage("Photo could not be saved locally. Reselect it after a reload; draft is not saved to inventory."); }
  }
}
async function purgeAccountPhotos(account) {
  if (!window.indexedDB) return;
  const db = await photoDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("photos", "readwrite");
    const request = tx.objectStore("photos").openCursor();
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor) { if (typeof cursor.key === "string" && cursor.key.startsWith(`${account}:`)) cursor.delete(); cursor.continue(); }
    };
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
  });
}
function clearDraft() {
  let cleared = true;
  const key = state.draft?.photoKey;
  if (state.account) {
    try { localStorage.removeItem(storageKey("draft")); } catch { cleared = false; toast("Could not clear local draft storage. Clear this site’s browser storage to remove it.", true); }
    if (key) trackPhoto(photoOperation("delete", photoStorageKey(key)).catch(() => toast("Could not remove local photo storage.", true)));
  }
  state.draft = null; state.draftPhoto = null; state.photoMissing = false; state.photoStorageFailed = false; state.photoSaving = false;
  return cleared;
}
function saveWorkspace() {
  if (!state.account) return;
  try { localStorage.setItem(storageKey("workspace"), JSON.stringify({ view: state.view || "identify", search: $("#search").value, filter: state.stockFilter, sort: state.receivedSort, scroll: window.scrollY })); } catch { /* optional */ }
}
function clearWorkspace() { if (state.account) try { localStorage.removeItem(storageKey("workspace")); } catch { return false; } return true; }
async function restoreWorkspace() {
  if (!state.account) { draftMessage("Local recovery unavailable: no signed-in account."); return; }
  try {
    const workspace = JSON.parse(localStorage.getItem(storageKey("workspace")) || "null");
    if (workspace && typeof workspace === "object") {
      $("#search").value = typeof workspace.search === "string" ? workspace.search : "";
      if (["all", "in", "out", "review"].includes(workspace.filter)) state.stockFilter = workspace.filter;
      state.receivedSort = [-1, 0, 1].includes(workspace.sort) ? workspace.sort : 0;
      $("#sort-received").textContent = state.receivedSort === -1 ? "Date received ↓" : state.receivedSort === 1 ? "Date received ↑" : "Date received ↕";
      $$("[data-stock-filter]").forEach((el) => { const active = el.dataset.stockFilter === state.stockFilter; el.classList.toggle("active", active); el.setAttribute("aria-pressed", String(active)); });
      switchView(["identify", "inventory", "warehouse", "reports", "settings", "studio"].includes(workspace.view) ? workspace.view : "identify", true);
      if (Number.isFinite(workspace.scroll)) requestAnimationFrame(() => window.scrollTo(0, workspace.scroll));
    }
    const draft = JSON.parse(localStorage.getItem(storageKey("draft")) || "null");
    if (!draft) return;
    if (draft.version !== 1 || !draft.fields || typeof draft.fields !== "object" || draftFields.some((key) => typeof draft.fields[key] !== "string") || typeof draft.fields.verified !== "boolean" || (draft.photoKey != null && typeof draft.photoKey !== "string") || (draft.fields.id && !/^\d+$/.test(draft.fields.id))) throw new Error("Invalid draft");
    const form = $("#part-form");
    form.reset();
    draftFields.forEach((key) => { form.elements[key].value = draft.fields[key]; });
    form.elements.verified.checked = draft.fields.verified;
    state.draft = draft;
    $("#drawer-title").textContent = draft.fields.id ? `Part #${draft.fields.id}` : "New part";
    $("#drawer").hidden = false;
    renderDrawerPhoto(draft.fields.image_path);
    draftMessage("Draft restored locally — not yet saved to inventory.");
    if (draft.photoKey) {
      state.photoMissing = true;
      try {
        const photo = await photoOperation("get", photoStorageKey(draft.photoKey));
        if (state.draft?.photoKey !== draft.photoKey) return;
        if (!(photo instanceof Blob)) throw new Error("Missing photo");
        state.draftPhoto = new File([photo], typeof draft.photoName === "string" ? draft.photoName : "photo", { type: photo.type });
        state.photoMissing = false;
        renderDrawerPhoto(draft.fields.image_path, state.draftPhoto);
      } catch { if (state.draft?.photoKey === draft.photoKey) draftMessage("Photo could not be recovered — reselect it before saving. Draft is not saved to inventory."); }
    }
    if (draft.fields.id) {
      try { await api(`/api/parts/${draft.fields.id}`); }
      catch { if (state.draft === draft) draftMessage("Could not verify this record exists. Your draft is retained; check inventory before saving."); }
    }
    if (draft.uncertainCreate) draftMessage("Previous creation may have completed. Check inventory before discarding this draft; retry is blocked to prevent duplicates.");
  } catch { draftMessage("Local recovery data is corrupted or unavailable; it has not been deleted."); toast("Could not restore local recovery data.", true); }
}

/* ── drawer / form ────────────────────────────────────────── */
function openDrawer(prefill = {}) {
  if (state.savingPart) return;
  if (state.draft) {
    // A closed drawer is a parked draft, not a discard.
    if (String(prefill.id || "") === String(state.draft.fields.id || "")) {
      $("#drawer").hidden = false;
      return;
    }
    if (!confirm("You have an unfinished local draft. Discard it and open another part?")) return;
    clearDraft();
  }
  const form = $("#part-form");
  form.reset();
  $("#drawer-title").textContent = prefill.id ? `Part #${prefill.id}` : "New part";
  const fit = (prefill.fitment && prefill.fitment[0]) || {};
  const values = {
    id: prefill.id || "",
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
  form.elements.part_image.value = "";
  renderDrawerPhoto(prefill.image_path || "");
  form.elements.verified.checked = Boolean(prefill.verified);
  $("#drawer").hidden = false;
  state.draftPhoto = null;
  state.photoMissing = false;
  saveDraft();
  form.elements.part_name.focus();
}

function closeDrawer() { if (state.savingPart) return; if (!$("#drawer").hidden) { saveDraft(); $("#drawer").hidden = true; } }

function renderDrawerPhoto(path, localFile = null) {
  const preview = $("#part-photo-preview");
  if (localFile) {
    preview.innerHTML = `<span class="muted small">Selected photo: ${escapeHtml(localFile.name)}</span><br><img class="drawer-photo-preview" src="${URL.createObjectURL(localFile)}" alt="Selected part photo preview" />`;
    preview.hidden = false;
  } else if (path) {
    preview.innerHTML = `<span class="muted small">Current photo</span><br><img class="drawer-photo-preview" src="${escapeHtml(thumbUrl(path))}" alt="Current part photo" />`;
    preview.hidden = false;
  } else {
    preview.innerHTML = "";
    preview.hidden = true;
  }
}

let draftPhotoPickToken = 0;
$("#part-form").elements.part_image.addEventListener("change", async (event) => {
  const token = ++draftPhotoPickToken;
  const picked = event.target.files?.[0];
  const path = $("#part-form").elements.image_path.value;
  const oldKey = state.draft?.photoKey;
  if (oldKey && state.account) trackPhoto(photoOperation("delete", photoStorageKey(oldKey)).catch(() => toast("Could not remove previous local photo.", true)));
  // Downscale now so the local draft, the preview and the eventual upload are
  // all one small JPEG.
  const file = picked ? await prepareImageForUpload(picked) : null;
  if (token !== draftPhotoPickToken) return; // a newer pick took over
  state.draftPhoto = file || null;
  state.photoMissing = false;
  state.photoStorageFailed = false;
  state.photoSaving = !!file;
  if (state.draft) { state.draft.photoKey = file ? `${Date.now()}-${Math.random()}` : null; state.draft.photoName = file?.name || "photo"; }
  saveDraft();
  if (file) trackPhoto(storePhoto(state.draft?.photoKey, file));
  renderDrawerPhoto(path, file || null);
});

const splitList = (value) => String(value || "").split(",").map((s) => s.trim()).filter(Boolean);

async function savePart(event) {
  event.preventDefault();
  const form = event.target;
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton?.disabled) return;
  if (state.photoMissing) { toast("Reselect the missing photo before saving, or discard the draft.", true); return; }
  if (state.draft?.uncertainCreate) { toast("The previous create may have completed. Check inventory before discarding this draft; it cannot be safely retried automatically.", true); return; }
  state.savingPart = true;
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
      if (state.draft) { state.draft.uncertainCreate = true; saveDraft(); }
      saved = await api("/api/parts", { method: "POST", body: JSON.stringify(payload) });
      form.elements.id.value = saved.id;
      if (state.draft) state.draft.uncertainCreate = false;
      saveDraft();
      $("#drawer-title").textContent = `Part #${saved.id}`;
    }
    const photo = state.draftPhoto || form.elements.part_image.files?.[0] || null;
    if (photo) {
      const upload = await prepareImageForUpload(photo);
      const imageForm = new FormData();
      imageForm.append("file", upload, upload.name || "part-photo.jpg");
      saved = await api(`/api/parts/${saved.id}/image`, { method: "POST", body: imageForm });
    }
    clearDraft();
    $("#drawer").hidden = true;
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
    if (state.draft?.uncertainCreate && error.status >= 400 && error.status < 500) state.draft.uncertainCreate = false;
    saveDraft();
    toast(error.message + (state.draft?.uncertainCreate ? " Check inventory before retrying: creation may have succeeded." : ""), true);
  } finally {
    state.savingPart = false;
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

function switchView(name, restoring = false) {
  if (!$( `.tab[data-view="${name}"]`)) return;
  state.view = name;
  if (!restoring) saveWorkspace();
  $$(".tab").forEach((tab) => {
    const selected = tab.dataset.view === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  const descriptions = {
    identify: ["Identify a part", "Start with a photo. Find the details that matter.", "PHOTO → REVIEW → STOCK"],
    inventory: ["Your inventory", "Every OEM number, shelf and stock level. In one place.", "FIND → VERIFY → ORGANISE"],
    warehouse: ["Warehouse", "Record delivery handovers and trace stock issued in FIFO order.", "FIND → HAND OVER → RECORD"],
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
  if (name === "warehouse") { searchWarehouse(); loadWarehouseHistory(); }
  if (name === "studio") populateStudioSelect();
  if (!restoring) window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ── warehouse delivery handovers ─────────────────────────── */
let warehousePart = null;
let warehouseLines = [];
let warehouseSearchToken = 0;
let warehouseHistoryToken = 0;
async function searchWarehouse() {
  const token = ++warehouseSearchToken;
  const query = $("#warehouse-search").value.trim();
  try {
    const parts = await api(`/api/parts?q=${encodeURIComponent(query)}&limit=100`);
    if (token !== warehouseSearchToken) return;
    const box = $("#warehouse-results");
    box.innerHTML = parts.length ? parts.map((part) => `<button type="button" class="btn ghost small-btn" data-warehouse-part="${part.id}">${escapeHtml(part.part_name)} · ${escapeHtml(part.oem_part_number || "no OEM")} · ${part.quantity} in stock</button>`).join("") : '<p class="muted small">No matching parts.</p>';
    $$("[data-warehouse-part]", box).forEach((button) => button.addEventListener("click", () => selectWarehousePart(Number(button.dataset.warehousePart))));
  } catch (error) { $("#warehouse-status").textContent = error.message; }
}
async function selectWarehousePart(id) {
  try {
    const result = await api(`/api/warehouse/parts/${id}/fifo`);
    warehousePart = result.part;
    $("#warehouse-selected").hidden = false;
    $("#warehouse-add-row").hidden = false;
    $("#warehouse-selected").innerHTML = `<strong>${escapeHtml(result.part.part_name)}</strong><p class="small muted">OEM ${escapeHtml(result.part.oem_part_number || "—")} · bin ${escapeHtml(result.part.bin_location || "unassigned")} · ${result.part.quantity} available</p>`;
    $("#warehouse-lots").innerHTML = `<p><strong>FIFO stock, oldest first</strong></p>${result.lots.length ? result.lots.map((lot) => `<p>${escapeHtml(lot.received_at || "Existing stock — receipt date unknown")} · ${lot.remaining} available</p>`).join("") : "<p>Out of stock.</p>"}${result.has_unknown_history ? "<p>Some older stock has no reliable receipt history. Its age cannot be verified.</p>" : ""}`;
    $("#warehouse-add-qty").value = "1";
    $("#warehouse-add-qty").max = result.part.quantity;
    $("#warehouse-add").disabled = result.part.quantity < 1;
  } catch (error) { toast(error.message, true); }
}
function renderWarehouseLines() {
  const box = $("#warehouse-lines");
  box.innerHTML = warehouseLines.length ? `<strong>Parts on this order</strong>${warehouseLines.map((line) => `<div class="row gap wrap"><span>${escapeHtml(line.part.part_name)} · ${escapeHtml(line.part.oem_part_number || "no OEM")} · quantity ${line.quantity}</span><button type="button" class="btn ghost small-btn" data-remove-warehouse="${line.part.id}">Remove</button></div>`).join("")}` : "No parts added yet.";
  $$("[data-remove-warehouse]", box).forEach((button) => button.addEventListener("click", () => { warehouseLines = warehouseLines.filter((line) => line.part.id !== Number(button.dataset.removeWarehouse)); renderWarehouseLines(); }));
}
function addWarehousePart() {
  const quantity = Number($("#warehouse-add-qty").value);
  if (!warehousePart || !Number.isSafeInteger(quantity) || quantity < 1 || quantity > warehousePart.quantity) { toast("Enter a valid quantity within available stock", true); return; }
  if (warehouseLines.some((line) => line.part.id === warehousePart.id)) { toast("Part already added. Remove it first to change the quantity.", true); return; }
  warehouseLines.push({ part: warehousePart, quantity });
  renderWarehouseLines();
}
async function loadWarehouseHistory() {
  const token = ++warehouseHistoryToken;
  try {
    const q = $("#warehouse-history-search").value.trim();
    const orders = await api(`/api/warehouse/delivery-orders?limit=100&q=${encodeURIComponent(q)}`);
    if (token !== warehouseHistoryToken) return;
    $("#warehouse-history").innerHTML = orders.length ? orders.map((order) => `<article class="warehouse-issue"><strong>Delivery order ${escapeHtml(order.order_number)}</strong><p class="small muted">Handed over ${escapeHtml(order.handed_over_at)}<br />Collected by ${escapeHtml(order.collected_by)} · Recorded by ${escapeHtml(order.recorded_by)}${order.note ? `<br />${escapeHtml(order.note)}` : ""}</p>${order.lines.map((line) => `<p class="small"><strong>${escapeHtml(line.part_name)}</strong> · ${escapeHtml(line.oem_part_number || "no OEM")} · ${line.quantity}<br /><span class="muted">FIFO: ${line.allocations.map((a) => `${a.quantity} from ${escapeHtml(a.received_at || "unknown opening stock")}`).join("; ")}</span></p>`).join("")}</article>`).join("") : '<p class="muted small">No matching delivery orders.</p>';
  } catch (error) { $("#warehouse-history").textContent = error.message; }
}
async function submitWarehouseIssue(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const number = form.elements.order_number.value.trim();
  const collector = form.elements.collected_by.value.trim();
  if (!number || !collector || !warehouseLines.length) { toast("Add parts, a delivery order number and the collector’s name", true); return; }
  if (!confirm(`Confirm handover of ${warehouseLines.length} part line(s) on ${number} to ${collector}? Stock will be deducted oldest first.`)) return;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await api("/api/warehouse/delivery-orders", { method: "POST", body: JSON.stringify({ order_number: number, collected_by: collector, note: form.elements.note.value.trim(), lines: warehouseLines.map((line) => ({ part_id: line.part.id, quantity: line.quantity })) }) });
    form.reset(); warehouseLines = []; renderWarehouseLines();
    $("#warehouse-status").textContent = `Delivery order ${number} saved.`;
    await Promise.all([warehousePart ? selectWarehousePart(warehousePart.id) : Promise.resolve(), searchWarehouse(), loadWarehouseHistory(), loadParts()]);
    toast("Delivery handover recorded");
  } catch (error) { $("#warehouse-status").textContent = error.message; toast(error.message, true); }
  finally { button.disabled = false; }
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
    saveWorkspace();
  }));
  $("#btn-signout").addEventListener("click", async () => {
    if (state.savingPart) { toast("Wait for the part save to finish before signing out.", true); return; }
    try {
      const draftCleared = clearDraft();
      const workspaceCleared = clearWorkspace();
      if (!draftCleared || !workspaceCleared) throw new Error("Browser storage could not be cleared");
      $("#drawer").hidden = true;
      await Promise.all([...pendingPhotoOperations]);
      await purgeAccountPhotos(state.account);
      state.account = null;
      await api("/api/auth/signout", { method: "POST" });
      window.location.assign("/login");
    } catch (error) { toast(`Sign out could not finish clearing local data: ${error.message}. Clear this site’s browser storage and try again.`, true); }
  });
  $("#shop-settings-form").addEventListener("submit", saveShopSettings);
  let warehouseTimer;
  $("#warehouse-search").addEventListener("input", () => { clearTimeout(warehouseTimer); warehouseTimer = setTimeout(searchWarehouse, 220); });
  $("#warehouse-form").addEventListener("submit", submitWarehouseIssue);
  $("#warehouse-add").addEventListener("click", addWarehousePart);
  let historyTimer;
  $("#warehouse-history-search").addEventListener("input", () => { clearTimeout(historyTimer); historyTimer = setTimeout(loadWarehouseHistory, 220); });
  $("#warehouse-refresh").addEventListener("click", loadWarehouseHistory);
  $("#part-form").addEventListener("submit", savePart);
  $("#part-form").addEventListener("input", saveDraft);
  $("#part-form").addEventListener("change", (event) => { if (event.target.name !== "part_image") saveDraft(); });
  $("#discard-draft").addEventListener("click", () => {
    if (state.savingPart) return;
    if (!confirm("Discard this local draft? Unsaved changes will be lost.")) return;
    clearDraft();
    $("#drawer").hidden = true;
  });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") { saveDraft(); saveWorkspace(); } });
  window.addEventListener("pagehide", () => { saveDraft(); saveWorkspace(); });
  window.addEventListener("scroll", saveWorkspace, { passive: true });
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
    saveWorkspace();
    timer = setTimeout(() => loadParts(event.target.value), 220);
  });
}

wireDropzone();
wireUi();
boot();
