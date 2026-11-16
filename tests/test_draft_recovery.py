"""Guards for mobile tab-switch / page-reload draft recovery.

The workspace is plain HTML + vanilla JS with no build step, so the contract
lives in three static files. `scripts/ui_draft_reload.cjs` proves the behaviour
end to end against a running server; these checks keep the contract from
silently drifting in a plain `pytest` run, which needs neither Node nor a
server.

The invariants that matter most to the owner collecting stock on a phone:

* the active tab, the unfinished part form and its photo survive a reload;
* a recovered draft is never submitted by itself and never duplicates a record;
* the draft is cleared only after a successful save or an explicit discard;
* drafts are scoped to the signed-in account and wiped on sign-out;
* unavailable storage, corrupted drafts and lost photos are reported honestly.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "web/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
DRAFTS_JS = (ROOT / "web/static/drafts.js").read_text(encoding="utf-8")
CSS = (ROOT / "web/static/styles.css").read_text(encoding="utf-8")


def _slice(start_marker: str, end_marker: str, source: str = APP_JS) -> str:
    """The source between two markers, so checks stay local to one function."""
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


# ── wiring ──────────────────────────────────────────────────────────────────
def test_draft_library_loads_before_the_app():
    scripts = re.findall(r'<script src="(/static/[\w.]+)"></script>', HTML)
    assert scripts == ["/static/drafts.js", "/static/app.js"], scripts
    assert (ROOT / "web/static/drafts.js").exists()
    assert "window.VerifixDrafts" in DRAFTS_JS
    assert "const drafts = window.VerifixDrafts || null;" in APP_JS


def test_drawer_exposes_draft_indicator_and_explicit_discard():
    drawer = _slice('<!-- part editor drawer -->', '<div id="toast"', HTML)
    assert 'id="drawer-draft-status"' in drawer
    assert 'id="btn-discard-draft"' in drawer
    # The indicator is announced, and starts hidden rather than claiming a draft.
    assert 'role="status"' in drawer and 'aria-live="polite"' in drawer
    assert re.search(r'id="drawer-draft-status"[^>]*hidden', drawer)
    assert re.search(r'id="btn-discard-draft"[^>]*hidden', drawer)
    assert ".draft-status" in CSS


def test_settings_offers_an_off_switch_for_on_device_drafts():
    assert 'id="keep-drafts"' in HTML
    assert "Keep drafts on this device" in HTML
    # The toggle sits outside the shop-details form so it is never POSTed to
    # /api/settings as a shop profile field.
    settings_form = _slice('<form id="shop-settings-form"', "</form>", HTML)
    assert "keep-drafts" not in settings_form


# ── requirement 3: saved as fields change, and on hide/kill ─────────────────
def test_draft_is_saved_while_typing_not_only_on_unload():
    wiring = _slice("function wireDraftPersistence()", "function syncDraftPrefUi()")
    assert 'form.addEventListener("input", schedulePartDraft' in wiring
    assert 'form.addEventListener("change", schedulePartDraft' in wiring
    # Debounced, so a keystroke storm does not hammer storage…
    assert "partDraftTimer = setTimeout(capturePartDraft" in APP_JS
    # …but a backgrounded or killed tab flushes immediately.
    for event in ("visibilitychange", "pagehide", "beforeunload"):
        assert event in wiring, event
    assert "flushPartDraft();" in wiring and "persistUiState();" in wiring


def test_photo_choice_is_persisted_as_soon_as_it_is_made():
    listener = _slice('$("#part-form").elements.part_image.addEventListener', "/* ── part-form draft")
    assert 'drafts.putPhoto("part", file)' in listener
    assert "capturePartDraft();" in listener


# ── requirements 1, 4, 10: what a reload restores ───────────────────────────
def test_boot_restores_tab_filters_form_and_scan_after_the_session_is_known():
    boot = _slice("async function boot()", "function renderProviderBadge(")
    # Account scoping must be resolved before anything is read back.
    assert boot.index("api(\"/api/auth/session\")") < boot.index("restoreUiDraft()")
    assert "drafts.setAccount(session.email" in boot
    for restore in ("restoreUiDraft()", "restoreScanDraft()", "restorePartDraft()"):
        assert restore in boot, restore
    # Each restore is individually guarded: a bad draft must not break boot.
    assert boot.count("catch") >= 3


def test_ui_restore_covers_tab_search_filter_and_sort():
    restore = _slice("function restoreUiDraft()", "function restoreScanDraft()")
    assert "switchView(ui.view)" in restore
    assert '$("#search").value = ui.search' in restore
    assert "state.stockFilter = ui.stockFilter" in restore
    assert "state.receivedSort = ui.receivedSort" in restore
    assert "syncDraftPrefUi()" in restore


def test_switch_view_persists_and_restores_scroll_per_tab():
    switch = _slice("function switchView(name)", "/* ── local draft restore")
    assert "drafts.saveUi({ view: name })" in switch
    assert "scrollMemory()" in switch and "currentScrollY()" in switch
    assert "state.view = name" in switch


def test_completed_identification_is_restored_without_being_re_run():
    restore = _slice("function restoreScanDraft()", "function wireDraftPersistence()")
    # Re-render only: the restore path must not call the identify endpoint.
    assert "/api/identify" not in restore
    assert "renderReport(result)" in restore and "renderMatch(result)" in restore
    assert "it was not re-run" in restore
    # An already-rendered scan is never replaced by an older stored one.
    assert "if (!stored || state.lastScan) return;" in restore
    # Restoring the photo must not re-trigger auto-detect through selectFile().
    assert "selectFile(" not in restore
    assert "state.selectedFile = file;" in restore


# ── requirement 9: never auto-submit, never duplicate ───────────────────────
def test_nothing_is_submitted_automatically():
    assert ".submit()" not in APP_JS
    assert "requestSubmit(" not in APP_JS
    # A restore only re-opens the drawer; saving stays a user action.
    restore = _slice("async function restorePartDraft()", "function discardPartDraft()")
    assert 'method: "POST"' not in restore and 'method: "PATCH"' not in restore
    assert "savePart(" not in restore
    assert "openDrawer(" in restore


def test_restore_only_reopens_a_drawer_that_was_open():
    restore = _slice("async function restorePartDraft()", "function discardPartDraft()")
    assert "if (!draft || !ui.drawerOpen) return;" in restore


def test_created_record_id_is_captured_before_the_photo_upload():
    save = _slice("async function savePart(event)", "/* ── studio ──")
    created = save.index('saved = await api("/api/parts"')
    recorded = save.index("form.elements.id.value = saved.id;")
    photo = save.index("/image`")
    assert created < recorded < photo, "a photo-upload failure must retry as an update"


# ── requirement 5: photo recovery is real and honest ────────────────────────
def test_recovered_photo_reaches_the_upload_and_is_never_faked():
    save = _slice("async function savePart(event)", "/* ── studio ──")
    assert "form.elements.part_image.files?.[0] || state.partDraftFile" in save

    attach = _slice("function attachFileToInput(", "async function restoreDraftPhoto(")
    assert "window.DataTransfer" in attach
    # Read-only `files` (jsdom, some engines) falls back to a marked accessor…
    assert 'input.dataset.syntheticFiles = "1"' in attach
    assert "function clearFileInput(" in APP_JS
    assert "delete input.files;" in APP_JS

    restore_photo = _slice("async function restoreDraftPhoto(", "async function restorePartDraft()")
    # …and every failure path says so instead of implying a recovered photo.
    assert "could not be recovered" in restore_photo
    assert "Photo recovered in memory" in restore_photo
    assert restore_photo.index("const attached = attachFileToInput(") < restore_photo.index("if (!attached)")


def test_stale_or_deleted_record_becomes_a_new_part_draft():
    restore = _slice("async function restorePartDraft()", "function discardPartDraft()")
    assert "no longer exists" in restore
    assert 'form.elements.id.value = "";' in restore
    assert "drafts.savePartDraft(draftSnapshot())" in restore


# ── requirement 7: cleared only on success or explicit discard ──────────────
def test_draft_is_cleared_only_after_a_successful_save():
    save = _slice("async function savePart(event)", "/* ── studio ──")
    try_block = save[: save.index("} catch (error) {")]
    catch_block = save[save.index("} catch (error) {") :]
    assert "drafts.clearPartDraft()" in try_block
    assert "draftSavedState = true;" in try_block
    # The failure path keeps the work and says so.
    assert "clearPartDraft" not in catch_block
    assert "flushPartDraft();" in catch_block
    assert "draft is kept" in catch_block


def test_closing_the_drawer_keeps_the_draft():
    close = _slice("function closeDrawer()", "function renderDrawerPhoto(")
    assert "flushPartDraft();" in close
    assert "clearPartDraft" not in close and "form.reset()" not in close
    assert 'drafts?.saveUi({ drawerOpen: false })' in close


def test_discard_requires_confirmation():
    discard = _slice("function discardPartDraft()", "function clearPartDraftState()")
    assert "window.confirm(" in discard
    assert "if (!proceed) return;" in discard
    assert "clearPartDraftState();" in discard


def test_opening_another_record_cannot_silently_replace_a_draft():
    open_drawer = _slice("function openDrawer(prefill = {}", "function closeDrawer()")
    assert "hasStoredDraft()" in open_drawer
    assert "window.confirm(" in open_drawer
    assert "if (!proceed) return;" in open_drawer
    # Parked work is protected even while the drawer is closed.
    assert "function hasStoredDraft()" in APP_JS
    dirty = _slice("function partDraftDirty()", "function setDraftStatus(")
    assert "drawerElement().hidden" in dirty and "hasStoredDraft()" in dirty


# ── requirement 8: per-account, wiped on logout, defensive ──────────────────
def test_drafts_are_scoped_to_the_signed_in_account():
    assert 'const UI_PREFIX = `verifix.drafts.${VERSION}.`;' in DRAFTS_JS
    assert "const uiKey = () => `${UI_PREFIX}${account}`;" in DRAFTS_JS
    assert "const photoKey = (name) => `${account}::${name}`;" in DRAFTS_JS
    assert "async function setAccount(email)" in DRAFTS_JS
    assert 'account = clean ?' in DRAFTS_JS


def test_explicit_logout_wipes_that_account_only():
    signout = _slice('$("#btn-signout").addEventListener', '$("#shop-settings-form")')
    assert "drafts.clearAccount()" in signout
    # The wipe happens before the request, so it cannot be skipped by a failure.
    assert signout.index("clearAccount()") < signout.index("/api/auth/signout")
    clear = _slice("async function clearAccount()", "function storageStatus()", DRAFTS_JS)
    assert "removeRaw(uiKey())" in clear
    assert 'removePhoto(name)' in clear
    # An expired session is not a logout: the 401 path must not wipe drafts.
    assert "clearAccount" not in _slice("async function api(path", "function renderMarkdown(")


def test_storage_failures_and_corrupted_drafts_are_handled():
    # Corrupted JSON is dropped, never applied.
    assert "catch { removeRaw(key); return null; }" in DRAFTS_JS
    # A quota/blocked write degrades to memory instead of throwing at the user.
    assert "memoryStore.set(key, value);" in DRAFTS_JS
    assert "catch { /* quota/blocked → memory only */ }" in DRAFTS_JS
    # Every restored value passes a type/whitelist sanitizer.
    for sanitizer in ("function sanitizeUi(", "function sanitizeDraft(", "function sanitizeScan("):
        assert sanitizer in DRAFTS_JS, sanitizer
    assert "function draftIsEmpty(" in DRAFTS_JS
    assert "drafts._internal.draftIsEmpty" in APP_JS
    # Photo bytes are recognised across JS realms, not by constructor identity.
    assert "ArrayBuffer.isView(data)" in DRAFTS_JS


def test_writes_stop_when_the_owner_turns_drafts_off():
    for fn in ("function saveUi(patch)", "function savePartDraft(draft)", "function saveScan(result)"):
        # "\n  }" is the function's own closing brace (the IIFE body is indented).
        body = _slice(fn, "\n  }", DRAFTS_JS)
        assert "draftsEnabled()" in body, fn
    assert "async function putPhoto(name, file)" in DRAFTS_JS
    put = _slice("async function putPhoto(name, file)", "async function getPhoto(", DRAFTS_JS)
    assert "draftsEnabled()" in put
    # Deleting still works while disabled, and must not resurrect an empty key.
    assert "function dropSection(section)" in DRAFTS_JS
    assert "removeRaw(uiKey());" in DRAFTS_JS
    assert "capturePartDraft" in APP_JS and "if (!drafts.draftsEnabled()) return;" in APP_JS


# ── requirement 6 + 12: honest indicator, desktop unaffected ────────────────
def test_indicator_states_that_a_draft_is_not_inventory():
    assert "Draft saved locally — not yet saved to inventory." in APP_JS
    assert "not yet saved to inventory." in _slice("function draftRestoredText(", "function capturePartDraft(")
    assert "Draft kept in memory — local storage is full or blocked." in APP_JS
    assert "function setDraftStatus(" in APP_JS


def test_desktop_flows_are_untouched_by_draft_recovery():
    # The library is optional: without it the app still runs (no build step,
    # and an older cached page must not break the shop's desktop).
    assert "const drafts = window.VerifixDrafts || null;" in APP_JS
    assert "if (!drafts) return;" in APP_JS
    assert "drafts?." in APP_JS
    # Existing behaviour that must not regress.
    assert '$("#part-form").addEventListener("submit", savePart);' in APP_JS
    assert 'if (event.key === "Escape") closeDrawer();' in APP_JS
    assert "$$(\"[data-close]\").forEach((el) => el.addEventListener(\"click\", closeDrawer));" in APP_JS
    assert 'window.scrollTo({ top: Number(target) || 0' in APP_JS
