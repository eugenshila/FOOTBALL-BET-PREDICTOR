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
    document.title = `${config.shop_name} — Parts workspace`;
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
    ? mediaUrl(part.image_path)
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
    const stats = await api("/api/stats");
    renderStats(stats);
  } catch (error) {
    toast(error.message, true);
  }
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
  $("#parts-empty").hidden = parts.length > 0;
  body.innerHTML = parts.map((part) => {
    const status = part.quantity <= 0
      ? '<span class="tag out">out</span>'
      : part.quantity <= 3
        ? `<span class="tag low">low · ${part.quantity}</span>`
        : `<span class="tag in">in stock</span>`;
    const verified = part.verified ? "" : ' <span class="tag unverified">unverified</span>';
    return `<tr data-id="${part.id}">
      <td><div class="part-cell">
        ${mediaUrl(part.image_path) ? `<img class="part-thumb" src="${escapeHtml(mediaUrl(part.image_path))}" alt="" loading="lazy" />` : '<span class="part-placeholder" aria-hidden="true">◇</span>'}
        <div><div class="part-name">${escapeHtml(part.part_name)}</div>
        <div class="part-sub">${escapeHtml(part.category || "")}${part.fitment[0] ? " · " + escapeHtml([part.fitment[0].make, part.fitment[0].model].filter(Boolean).join(" ")) : ""}</div></div>
      </div></td>
      <td class="mono">${escapeHtml(part.oem_part_number || "—")}</td>
      <td>${escapeHtml(part.bin_location || "—")}</td>
      <td class="num">${part.quantity}</td>
      <td class="num">${escapeHtml(money(part.unit_price, part.currency))}</td>
      <td>${status}${verified}</td>
      <td class="num">
        <button class="btn tiny" data-card="${part.id}" title="Generate promo cards">Cards</button>
        <button class="btn tiny" data-edit="${part.id}" title="Edit">Edit</button>
        <button class="btn tiny" data-images="${part.id}" title="Review image candidates">Photos</button>
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
let csvBusy = false;

async function sendCsv(file, dryRun) {
  if (csvBusy) return;
  switchView("inventory");
  const isZip = /\.zip$/i.test(file.name);
  if (!/\.(csv|zip)$/i.test(file.name) || file.size > (isZip ? 32 : 8) * 1024 * 1024) {
    toast("Choose a CSV up to 8 MB or a ZIP up to 32 MB.", true);
    return;
  }
  csvBusy = true;
  pendingCsv = null;
  $("#import-actions").innerHTML = "";
  $("#csv-dropzone").disabled = true;
  $("#btn-import").disabled = true;
  const block = $("#import-block");
  const summary = $("#import-summary");
  block.hidden = false;
  summary.innerHTML = `<p class="status busy">${dryRun ? "Checking" : "Importing"} ${escapeHtml(file.name)}…</p>`;

  const form = new FormData();
  form.append("file", file);

  try {
    const query = `?dry_run=${dryRun}&update_existing=false&review_images=true`;
    const result = await api(`/api/inventory/import${query}`, { method: "POST", body: form });
    renderImportSummary(result, file, dryRun);
    if (!dryRun) {
      pendingCsv = null;
      await loadParts($("#search").value);
      populateStudioSelect();
      toast(`Imported ${result.created} part(s)`);
      showImageReview(result.results.filter((r) => r.part_id));
    } else {
      pendingCsv = file;
    }
  } catch (error) {
    summary.innerHTML = `<p class="status error">${escapeHtml(error.message)}</p>`;
  } finally {
    csvBusy = false;
    $("#csv-dropzone").disabled = false;
    $("#btn-import").disabled = false;
  }
}

function renderImportSummary(result, file, dryRun) {
  const summary = $("#import-summary");
  const actions = $("#import-actions");
  const stat = (label, value, cls = "") =>
    `<div class="${cls}"><strong>${value}</strong>${label}</div>`;

  summary.innerHTML = `
    <p class="muted small">${escapeHtml(file.name)} · ${result.rows_total} rows read${dryRun ? " · dry run, nothing written yet. Photo paths are deferred for approval" : ""}</p>
    ${result.bundle ? `<p class="status">${result.bundle.images_matched} images matched by part number · ${result.bundle.images_missing} rows without an image · ${result.bundle.unused_images.length} unused images. Photos require individual approval.${result.bundle.test_only ? " TEST PACKAGE: these images are labelled placeholders, not part photos." : ""}</p>` : ""}
    <div class="import-stats">
      ${stat("new parts", result.created, "good")}
      ${stat("updated", result.updated)}
      ${stat("already in stock", result.skipped)}
      ${stat("problems", result.errors, result.errors ? "bad" : "")}
    </div>
    ${result.problems.length ? `<ul class="import-problems">${result.problems.map((p) => `<li>row ${p.row}: ${escapeHtml(p.message)}</li>`).join("")}</ul>` : ""}
    <p class="import-cols">Columns mapped: ${Object.entries(result.columns_mapped).map(([from, to]) => `${escapeHtml(from)} → ${escapeHtml(to)}`).join(", ") || "none"}${result.ignored_columns.length ? ` · ignored: ${result.ignored_columns.map(escapeHtml).join(", ")}` : ""}</p>`;

  summary.insertAdjacentHTML("beforeend", `<div class="table-wrap"><table class="table"><thead><tr><th>Row</th><th>Part / OEM</th><th>Result</th></tr></thead><tbody>${result.results.map((r) => `<tr><td>${r.row}</td><td>${escapeHtml(r.part_name)}<div class="part-sub">${escapeHtml(r.oem_part_number)}</div>${r.image_filename ? `<div class="part-sub">Photo: ${escapeHtml(r.image_filename)}</div>` : ""}</td><td>${escapeHtml(r.status)}${r.message ? ` · ${escapeHtml(r.message)}` : ""}</td></tr>`).join("")}</tbody></table></div>${result.rows_total > result.results.length ? '<p class="muted small">Showing the first 500 results. Use Photos in inventory to review other rows.</p>' : ""}`);

  actions.innerHTML = dryRun
    ? `<button class="btn primary small-btn" id="btn-apply-import" ${result.created + result.updated === 0 && !(result.bundle?.images_matched && result.skipped) ? "disabled" : ""}>Apply import</button>
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

/* ── navigation ───────────────────────────────────────────── */
function switchView(name) {
  $$(".tab").forEach((tab) => {
    const selected = tab.dataset.view === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  const descriptions = {
    identify: ["Identify a part", "Start with a photo. Find the details that matter.", "PHOTO → REVIEW → STOCK"],
    inventory: ["Your inventory", "Every OEM number, shelf and stock level. In one place.", "FIND → VERIFY → ORGANISE"],
    studio: ["Promo studio", "Turn verified stock into something worth sharing.", "CHOOSE → CREATE → SHARE"],
  };
  const [title, description, workflow] = descriptions[name] || descriptions.identify;
  $("#workspace-title").textContent = title;
  $("#workspace-description").textContent = description;
  $("#workspace-step").textContent = workflow;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  if (name === "inventory") loadParts($("#search").value);
  if (name === "studio") populateStudioSelect();
  window.scrollTo({ top: 0, behavior: "smooth" });
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
  }));
  $("#btn-signout").addEventListener("click", async () => {
    try {
      await api("/api/auth/signout", { method: "POST" });
      window.location.assign("/login");
    } catch (error) { toast(error.message, true); }
  });
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
    if (file) sendCsv(file, true);
  });
  $("#close-image-review").addEventListener("click", () => { $("#image-review").hidden = true; });
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
