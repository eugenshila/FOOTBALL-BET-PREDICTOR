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
    const [config, health, stats] = await Promise.all([
      api("/api/config"), api("/api/health"), api("/api/stats"),
    ]);
    state.config = config;
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
      ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(part.part_name)}" />` : ""}
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
  if (path.startsWith("/")) return path;
  if (path.startsWith("assets/seed/")) return `/api/demo-image/${path.split("/").pop().replace(/\.\w+$/, "")}`;
  const file = path.split("/").pop();
  if (path.startsWith("uploads/")) return `/media/uploads/${file}`;
  if (path.startsWith("exports/")) return `/media/exports/${file}`;
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
    const stats = await api("/api/stats");
    renderStats(stats);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderParts() {
  const body = $("#parts-body");
  const parts = state.parts;
  $("#parts-empty").hidden = parts.length > 0;
  body.innerHTML = parts.map((part) => {
    const status = part.quantity <= 0
      ? '<span class="tag out">out</span>'
      : part.quantity <= 3
        ? `<span class="tag low">low · ${part.quantity}</span>`
        : `<span class="tag in">in stock</span>`;
    const verified = part.verified ? "" : ' <span class="tag unverified">unverified</span>';
    return `<tr data-id="${part.id}">
      <td>
        <div class="part-name">${escapeHtml(part.part_name)}</div>
        <div class="part-sub">${escapeHtml(part.category || "")}${part.fitment[0] ? " · " + escapeHtml([part.fitment[0].make, part.fitment[0].model].filter(Boolean).join(" ")) : ""}</div>
      </td>
      <td class="mono">${escapeHtml(part.oem_part_number || "—")}</td>
      <td>${escapeHtml(part.bin_location || "—")}</td>
      <td class="num">${part.quantity}</td>
      <td class="num">${escapeHtml(money(part.unit_price, part.currency))}</td>
      <td>${status}${verified}</td>
      <td class="num">
        <button class="btn tiny" data-card="${part.id}" title="Generate promo cards">Cards</button>
        <button class="btn tiny" data-edit="${part.id}" title="Edit">Edit</button>
      </td>
    </tr>`;
  }).join("");

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
  form.elements.verified.checked = Boolean(prefill.verified);
  $("#drawer").hidden = false;
  form.elements.part_name.focus();
}

function closeDrawer() { $("#drawer").hidden = true; }

const splitList = (value) => String(value || "").split(",").map((s) => s.trim()).filter(Boolean);

async function savePart(event) {
  event.preventDefault();
  const form = event.target;
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
    if (id) {
      await api(`/api/parts/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
      toast("Part updated");
    } else {
      const created = await api("/api/parts", { method: "POST", body: JSON.stringify(payload) });
      toast(`Added part #${created.id}`);
    }
    closeDrawer();
    await loadParts($("#search").value);
    populateStudioSelect();
  } catch (error) {
    toast(error.message, true);
  }
}

/* ── studio ───────────────────────────────────────────────── */
function populateStudioSelect() {
  const select = $("#studio-part");
  const previous = select.value;
  select.innerHTML = state.parts
    .map((p) => `<option value="${p.id}">${escapeHtml(p.part_name)} — ${escapeHtml(p.oem_part_number || "no OEM")}${p.verified ? "" : " (unverified)"}</option>`)
    .join("");
  if (previous) select.value = previous;
}

function openStudioFor(partId) {
  switchView("studio");
  populateStudioSelect();
  $("#studio-part").value = String(partId);
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

  try {
    const result = await api("/api/cards", {
      method: "POST",
      body: JSON.stringify({
        part_id: Number(partId),
        formats,
        headline: $("#studio-headline").value.trim(),
      }),
    });
    const output = $("#card-output");
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
  }
}

/* ── CSV import ───────────────────────────────────────────── */
let pendingCsv = null;

async function sendCsv(file, dryRun) {
  const block = $("#import-block");
  const summary = $("#import-summary");
  block.hidden = false;
  summary.innerHTML = `<p class="status busy">${dryRun ? "Checking" : "Importing"} ${escapeHtml(file.name)}…</p>`;

  const form = new FormData();
  form.append("file", file);

  try {
    const query = `?dry_run=${dryRun}&update_existing=false`;
    const result = await api(`/api/inventory/import${query}`, { method: "POST", body: form });
    renderImportSummary(result, file, dryRun);
    if (!dryRun) {
      pendingCsv = null;
      await loadParts($("#search").value);
      populateStudioSelect();
      toast(`Imported ${result.created} part(s)`);
    } else {
      pendingCsv = file;
    }
  } catch (error) {
    summary.innerHTML = `<p class="status error">${escapeHtml(error.message)}</p>`;
  }
}

function renderImportSummary(result, file, dryRun) {
  const summary = $("#import-summary");
  const actions = $("#import-actions");
  const stat = (label, value, cls = "") =>
    `<div class="${cls}"><strong>${value}</strong>${label}</div>`;

  summary.innerHTML = `
    <p class="muted small">${escapeHtml(file.name)} · ${result.rows_total} rows read${dryRun ? " · dry run, nothing written yet" : ""}</p>
    <div class="import-stats">
      ${stat("new parts", result.created, "good")}
      ${stat("updated", result.updated)}
      ${stat("already in stock", result.skipped)}
      ${stat("problems", result.errors, result.errors ? "bad" : "")}
    </div>
    ${result.problems.length ? `<ul class="import-problems">${result.problems.map((p) => `<li>row ${p.row}: ${escapeHtml(p.message)}</li>`).join("")}</ul>` : ""}
    <p class="import-cols">Columns mapped: ${Object.entries(result.columns_mapped).map(([from, to]) => `${escapeHtml(from)} → ${escapeHtml(to)}`).join(", ") || "none"}${result.ignored_columns.length ? ` · ignored: ${result.ignored_columns.map(escapeHtml).join(", ")}` : ""}</p>`;

  actions.innerHTML = dryRun
    ? `<button class="btn primary small-btn" id="btn-apply-import">Apply import</button>
       <button class="btn ghost small-btn" id="btn-cancel-import">Cancel</button>`
    : `<button class="btn ghost small-btn" id="btn-cancel-import">Dismiss</button>`;

  if (dryRun) {
    $("#btn-apply-import").addEventListener("click", () => {
      if (pendingCsv) sendCsv(pendingCsv, false);
    });
  }
  $("#btn-cancel-import").addEventListener("click", () => {
    pendingCsv = null;
    $("#import-block").hidden = true;
    $("#csv-input").value = "";
  });
}

/* ── navigation ───────────────────────────────────────────── */
function switchView(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  if (name === "inventory") loadParts($("#search").value);
  if (name === "studio") populateStudioSelect();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function wireUi() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
  $("#part-form").addEventListener("submit", savePart);
  $$("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
  $("#btn-new-part").addEventListener("click", () => openDrawer({}));
  $("#btn-import").addEventListener("click", () => $("#csv-input").click());
  $("#csv-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) sendCsv(file, true);
    event.target.value = "";
  });
  $("#btn-generate").addEventListener("click", generateCards);
  $("#studio-part").addEventListener("change", () => { /* wait for the button */ });
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
boot();
