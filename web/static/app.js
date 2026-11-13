/* Parts ID Engine — vanilla JS, no build step, no external calls. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  config: null,
  parts: [],
  selectedFile: null,
  lastScan: null,
  currentPart: null,
  receivedSort: 0,
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
  if (response.status === 401) { location.assign("/login"); throw new Error("Your session has expired. Please sign in again."); }
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
    const [config, health, stats, session] = await Promise.all([
      api("/api/config"), api("/api/health"), api("/api/stats"), api("/api/auth/session"),
    ]);
    $("#auth-email").textContent = session.email || "";
    state.config = config;
    populateShopSettings(config);
    renderSocialLinks($("#studio-social-links"), config.social_links);
    $("#shop-name").textContent = config.shop_name;
    $("#shop-tagline").textContent = config.tagline;
    $("#max-mb").textContent = config.max_upload_mb;
    renderProviderBadge(health);
    renderStats(stats);
    await loadParts();
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
    $("#btn-adopt-report").addEventListener("click", () => openDrawer(prefillFromReport(result.report)));
    $$("[data-adopt]", body).forEach((button) =>
      button.addEventListener("click", () => openStudioFor(Number(button.dataset.adopt))));
    return;
  }

  badge.className = `badge ${part.verified ? "ok" : "warn"}`;
  badge.textContent = part.verified ? "verified stock record" : "unverified record";
  const imageUrl = part.image_path
    ? (part.image_path.startsWith("assets/seed/") ? `/api/demo-image/${part.image_path.split("/").pop().replace(/\.\w+$/, "")}` : part.image_path)
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
  const raw = String(path).trim();
  if (/^https?:\/\//i.test(raw)) return raw;
  const normalized = raw.replace(/\\/g, "/");
  if (/^[a-z]:\//i.test(normalized) || normalized.startsWith("//")) return "";
  if (normalized.startsWith("/")) return normalized;
  if (normalized.startsWith("assets/seed/")) return `/api/demo-image/${normalized.split("/").pop().replace(/\.\w+$/, "")}`;
  const file = normalized.split("/").pop();
  if (normalized.startsWith("uploads/")) return `/media/uploads/${file}`;
  if (normalized.startsWith("exports/")) return `/media/exports/${file}`;
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

function renderParts() {
  const body = $("#parts-body");
  const parts = [...state.parts];
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
    const status = part.quantity <= 0
      ? '<span class="tag out">out</span>'
      : part.quantity <= 3
        ? `<span class="tag low">low · ${part.quantity}</span>`
        : `<span class="tag in">in stock</span>`;
    const verified = part.verified ? "" : ' <span class="tag unverified">unverified</span>';
    const received = part.last_received_at
      ? new Date(part.last_received_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
      : "—";
    return `<tr data-id="${part.id}">
      <td>
        <div class="part-name">${escapeHtml(part.part_name)}</div>
        <div class="part-sub">${escapeHtml(part.category || "")}${part.fitment[0] ? " · " + escapeHtml([part.fitment[0].make, part.fitment[0].model].filter(Boolean).join(" ")) : ""}</div>
      </td>
      <td class="mono">${escapeHtml(part.oem_part_number || "—")}</td>
      <td>
        <input class="part-photo-input" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" data-photo-input="${part.id}" hidden />
        <button class="btn tiny" type="button" data-photo="${part.id}" title="${part.image_path ? "Replace" : "Add"} this part's photo">${part.image_path ? "Replace photo" : "Add photo"}</button>
        ${photoUrl ? `<a class="part-photo-link" href="${escapeHtml(photoUrl)}" target="_blank" rel="noopener" title="View current photo"><img class="part-photo-thumb" src="${escapeHtml(photoUrl)}" alt="Photo of ${escapeHtml(part.part_name)}" /></a>` : (part.image_path ? '<span class="image-fallback">Photo unavailable</span>' : "")}
      </td>
      <td>${escapeHtml(part.bin_location || "—")}</td>
      <td class="num">${part.quantity}</td>
      <td title="Latest recorded positive stock movement">${escapeHtml(received)}</td>
      <td class="num">${escapeHtml(money(part.unit_price, part.currency))}</td>
      <td>${status}${verified}</td>
      <td class="num">
        <button class="btn tiny" data-card="${part.id}" title="Generate promo cards">Cards</button>
        <button class="btn tiny" data-edit="${part.id}" title="Edit">Edit</button>
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
  $$("tr[data-id]", body).forEach((row) => row.addEventListener("click", () => {
    const record = state.parts.find((p) => p.id === Number(row.dataset.id));
    if (record) openDrawer(record);
  }));
}

$("#sort-received")?.addEventListener("click", () => {
  state.receivedSort = state.receivedSort === -1 ? 1 : -1;
  $("#sort-received").textContent = state.receivedSort === -1 ? "Date received ↓" : "Date received ↑";
  renderParts();
});

/* ── drawer / form ────────────────────────────────────────── */
function openDrawer(prefill = {}) {
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
  form.elements.part_name.focus();
}

function closeDrawer() { $("#drawer").hidden = true; }

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
  const file = event.target.files?.[0];
  const path = $("#part-form").elements.image_path.value;
  renderDrawerPhoto(path, file || null);
});

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
      form.elements.id.value = saved.id;
      $("#drawer-title").textContent = `Part #${saved.id}`;
    }
    const photo = form.elements.part_image.files?.[0];
    if (photo) {
      const imageForm = new FormData();
      imageForm.append("file", photo);
      saved = await api(`/api/parts/${saved.id}/image`, { method: "POST", body: imageForm });
    }
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
    toast(error.message, true);
  } finally {
    if (submitButton) { submitButton.disabled = false; submitButton.textContent = "Save part"; }
  }
}

/* ── studio ───────────────────────────────────────────────── */
function populateStudioSelect() {
  const selected = state.parts.find((part) => String(part.id) === $("#studio-part").value)
    || state.parts[0];
  if (selected) setStudioPart(selected);
}

function setStudioPart(part) {
  if (!state.parts.some((item) => item.id === part.id)) state.parts.unshift(part);
  $("#studio-part").value = String(part.id);
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
  if (!dryRun && stockImportInProgress) return;
  if (!dryRun) stockImportInProgress = true;
  const block = $("#import-block");
  const summary = $("#import-summary");
  block.hidden = false;
  summary.innerHTML = `<p class="status busy">${dryRun ? "Checking" : "Importing"} ${escapeHtml(file.name)}…</p>`;

  const form = new FormData();
  form.append("file", file);

  try {
    const query = `?dry_run=${dryRun}&update_existing=${updateExisting}`;
    const result = await api(`/api/inventory/import${query}`, { method: "POST", body: form });
    renderImportSummary(result, file, dryRun, updateExisting);
    if (!dryRun) {
      pendingStockFile = null;
      await loadParts($("#search").value);
      populateStudioSelect();
      toast(`Imported ${result.created} part(s)`);
    } else {
      pendingStockFile = file;
      pendingUpdateExisting = updateExisting;
    }
  } catch (error) {
    summary.innerHTML = `<p class="status error">${escapeHtml(error.message)}</p>`;
    const applyButton = $("#btn-apply-import");
    if (applyButton) { applyButton.disabled = false; applyButton.textContent = "Apply import"; }
  } finally {
    if (!dryRun) stockImportInProgress = false;
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
    <p class="muted small">${escapeHtml(file.name)} · ${result.rows_total} rows read${dryRun ? " · dry run, nothing written yet" : ""}</p>
    <div class="import-stats">
      ${stat("new parts", result.created, "good")}
      ${stat(mergeMatches ? "matched · qty added" : "updated", result.updated)}
      ${stat("already in stock", result.skipped)}
      ${stat("problems", result.errors, result.errors ? "bad" : "")}
    </div>
    ${mergeMatches ? '<p class="import-cols">Matching OEM or alternate numbers use the existing part record; uploaded quantities are added to current stock. The Excel file is not changed.</p>' : ""}
    ${result.images_found ? `<p class="import-cols">${dryRun ? `${result.images_found} embedded photo(s) found` : `${result.images_attached || 0} photo(s) attached`}</p>` : ""}
    ${aliasRows.length ? `<p class="import-cols">${aliasRows.length} row(s) whose OEM number is listed as an alternate/superseded number were kept as references and not added as separate parts: ${aliasRows.slice(0, 8).map((row) => `row ${row.row} · ${escapeHtml(row.oem_part_number)}`).join(", ")}${aliasRows.length > 8 ? ", …" : ""}</p>` : ""}
    ${result.problems.length ? `<ul class="import-problems">${result.problems.map((p) => `<li>row ${p.row}: ${escapeHtml(p.message)}</li>`).join("")}</ul>` : ""}
    <p class="import-cols">Columns mapped: ${Object.entries(result.columns_mapped).map(([from, to]) => `${escapeHtml(from)} → ${escapeHtml(to)}`).join(", ") || "none"}${result.ignored_columns.length ? ` · ignored: ${result.ignored_columns.map(escapeHtml).join(", ")}` : ""}</p>`;

  actions.innerHTML = dryRun
    ? `<button class="btn primary small-btn" id="btn-apply-import">Apply import</button>
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

function chooseStockFile(file) {
  if (file) sendStockFile(file, true, $("#import-update-existing").checked);
}

function openImportPanel() {
  const block = $("#import-block");
  block.hidden = false;
  block.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  if (name === "inventory") loadParts($("#search").value);
  if (name === "reports") loadVerificationSummary();
  if (name === "studio") populateStudioSelect();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function wireUi() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
  $("#shop-settings-form").addEventListener("submit", saveShopSettings);
  $("#part-form").addEventListener("submit", savePart);
  $$("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
  $("#btn-new-part").addEventListener("click", () => openDrawer({}));
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
    timer = setTimeout(() => loadParts(event.target.value), 220);
  });
}

wireDropzone();
wireUi();
$("#btn-signout").addEventListener("click", async () => {
  try { await fetch("/api/auth/signout", { method: "POST" }); } finally { location.assign("/login"); }
});
boot();
