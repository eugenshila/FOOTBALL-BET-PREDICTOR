"""Contract guards for local recovery of a completed identification.

The behavioural tests live in ``scripts/draft_recovery_test.cjs`` (jsdom + a real
reload against fake browser storage); run them with ``make ui-test``-style Node
commands documented in RUNNING.md. This module needs no Node and no server: it
pins the invariants that are easy to break silently while editing ``app.js``, so
a plain ``pytest`` run still fails if recovery stops being safe.
"""

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
APP_JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


def function_body(source: str, signature: str) -> str:
    """Return the body of the first function whose header contains ``signature``.

    Braces are counted so nested blocks and template literals containing ``{``
    do not truncate the slice (a naive "up to the first }" read does).
    """
    start = source.index(signature)
    open_at = source.index("{", start)
    depth = 0
    for index in range(open_at, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_at : index + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


@pytest.fixture(scope="module")
def restore_scan() -> str:
    return function_body(APP_JS, "async function restoreScan()")


def test_boot_restores_the_scan_after_parts_are_loaded():
    """The stale-match check compares against inventory, so order matters."""
    assert "await loadParts();" in APP_JS
    assert APP_JS.index("await loadParts();") < APP_JS.index("await restoreWorkspace();")
    assert APP_JS.index("await restoreWorkspace();") < APP_JS.index("await restoreScan();")


def test_a_completed_identification_is_persisted_with_its_photo():
    identify = function_body(APP_JS, "async function identify()")
    saved_at = identify.index("state.lastScan = result;")
    assert "saveScan();" in identify[saved_at:]
    assert "keepScanPhoto(state.selectedFile);" in identify[saved_at:]


def test_only_a_completed_identification_is_persisted():
    """An in-flight or failed request must leave nothing to restore."""
    identify = function_body(APP_JS, "async function identify()")
    failure = identify[identify.index("} catch (error) {") :]
    assert "saveScan()" not in failure
    assert "keepScanPhoto(" not in failure


def test_restore_never_starts_a_request(restore_scan):
    """Restoring re-renders locally; it must not re-run or re-upload anything."""
    for forbidden in ("identify()", "selectFile(", "api(", "fetch(", "submit(", "requestSubmit("):
        assert forbidden not in restore_scan, f"restoreScan calls {forbidden}"


def test_restore_refuses_to_clobber_a_live_result(restore_scan):
    assert restore_scan.startswith("{\n  if (!state.account || state.lastScan) return;")
    # The photo read is async: re-check identity before adopting it.
    assert "if (state.lastScan !== stored.result) return;" in restore_scan


def test_restore_validates_the_stored_shape_before_rendering(restore_scan):
    assert "stored.version !== 1" in restore_scan
    assert 'typeof report.overall_confidence !== "string"' in restore_scan
    assert restore_scan.index("stored.version !== 1") < restore_scan.index("renderReport(")


def test_a_missing_photo_is_disclosed_rather_than_implied(restore_scan):
    """Never tell the user a photo came back when it did not."""
    assert "if (!(photo instanceof Blob))" in restore_scan
    assert "photo is no longer on this device" in restore_scan
    assert "The photo came back too." in restore_scan


def test_an_out_of_date_stock_match_is_disclosed(restore_scan):
    assert "!state.parts.some((part) => part.id === matched.id)" in restore_scan
    assert "is no longer in inventory, so the match below is out of date" in restore_scan
    # The stale note must survive the later "photo recovered" line.
    assert "scanMessage(`${base} The photo came back too.`);" in restore_scan


def test_a_corrupt_report_is_cleared_without_being_re_run(restore_scan):
    assert 'localStorage.removeItem(storageKey("scan"))' in restore_scan
    assert "could not be read, so it was cleared" in restore_scan
    assert "Nothing was re-run" in restore_scan


def test_tab_switch_and_kill_flush_the_scan():
    for handler in ('document.addEventListener("visibilitychange"', 'window.addEventListener("pagehide"'):
        block = APP_JS[APP_JS.index(handler) :]
        block = block[: block.index("\n")]
        assert "saveScan()" in block, handler


def test_storage_failures_are_reported_without_hiding_the_result():
    """The "Done — … via gemini + web search" line must not be overwritten."""
    save_scan = function_body(APP_JS, "function saveScan()")
    keep_photo = function_body(APP_JS, "function keepScanPhoto(file)")
    assert "scanNote(" in save_scan and "scanMessage(" not in save_scan
    assert "scanNote(" in keep_photo and "scanMessage(" not in keep_photo
    note = function_body(APP_JS, "function scanNote(text)")
    assert "existing ? `${existing} ${text}` : text" in note
    assert "if (existing.includes(text)) return;" in note


def test_signing_out_clears_this_accounts_scan():
    handler = APP_JS[APP_JS.index('$("#btn-signout").addEventListener("click"') :]
    handler = handler[: handler.index("});")]
    assert "clearScan()" in handler
    # Both stores are cleared unconditionally — no `a() && b()` short-circuit that
    # would skip the scan when the workspace key could not be removed.
    assert "&& clearScan()" not in handler
    assert "clearWorkspace();" in handler and "clearScan();" in handler
    assert "purgeAccountPhotos(state.account)" in handler  # the photo blob too
    clear_scan = function_body(APP_JS, "function clearScan()")
    assert 'localStorage.removeItem(storageKey("scan"))' in clear_scan
    assert "state.lastScan = null;" in clear_scan
    assert "return false;" in clear_scan  # an uncleanable store blocks sign-out


def test_scan_storage_is_account_scoped():
    assert 'const scanPhotoKey = () => photoStorageKey("identify");' in APP_JS
    assert "photoStorageKey(key) { return `${state.account}:${key}`; }" in APP_JS
    assert "storageKey = (kind) => `verifix:${encodeURIComponent(state.account)}:${kind}:v1`" in APP_JS


def test_status_element_is_a_live_region():
    assert '<p id="identify-status" class="status" role="status">' in INDEX_HTML
