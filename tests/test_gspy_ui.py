"""Mixed notebooks, real CLI/kernel execution, portable logs, and restoration."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    work = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]) / "work"
    scientific = work / "results.gspy"
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        authenticate_browser(context, url)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#tab-new")
        page.wait_for_function("activeTab()?.format === 'ipynb'")
        ordinary = page.evaluate("active")
        ordinary_bytes = Path(ordinary).read_bytes()
        page.click("#tab-new")
        page.get_by_role("menuitem", name="Scientific notebook").click()
        page.fill("#ask-input", "results.gspy")
        page.click("#ask-ok")
        page.wait_for_function("activeTab()?.format === 'gspy' && cells.length === 1")
        assert page.evaluate("cells[0].cell_type") == "markdown"
        document_id = json.loads(scientific.read_text())["metadata"]["gusnotebook"]["document_id"]

        # Existing agent CLI commands target the new format unchanged.
        for source in ("seed = 40", "print(seed + 2)"):
            result = subprocess.run([sys.executable, "-m", "gusnotebook.cli", "-n", str(scientific),
                                     "add", "--no-run", source], capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
        page.wait_for_function("cells.length === 3 && document.querySelectorAll('.gspy-python').length === 2")
        page.evaluate("() => runAll()")
        expect(page.locator(".output.stream").last).to_contain_text("42")
        cid = page.evaluate("cells[2].id")
        path = page.evaluate("active")
        response = context.request.delete(url + "api/cells/" + cid, params={"notebook": path})
        assert response.ok, response.text()
        page.wait_for_function("cells.length === 2")

        page.click("#workspace-more")
        page.get_by_role("menuitem", name="Document history", exact=True).click()
        deletion = page.locator('#document-history-list [data-event-kind="delete"]')
        expect(deletion).to_be_visible()
        deletion.get_by_role("button", name="Restore block").click()
        page.wait_for_function("cells.length === 3")
        expect(page.locator('#document-history-list [data-event-kind="restore"]')).to_be_visible()
        assert page.evaluate("id => getCell(id).outputs.length", cid) == 0
        with page.expect_download() as download:
            page.get_by_role("button", name="Export full log (.jsonl)").click()
        records = [json.loads(line) for line in Path(download.value.path()).read_text().splitlines()]
        assert download.value.suggested_filename == "results.history.jsonl"
        assert records[0]["document_id"] == document_id
        assert any(record.get("kind") == "delete" for record in records)
        assert any(record.get("kind") == "restore" for record in records)
        assert any(record["record"] == "artifact" and "42" in json.dumps(record["data"]) for record in records)
        page.screenshot(path=str(Path(tempfile.gettempdir()) / "gusnb-gspy-history.png"))
        page.keyboard.press("Escape")
        expect(page.locator("#document-history-back")).not_to_be_visible()
        page.evaluate("id => runCell(id)", cid)
        expect(page.locator(".output.stream").last).to_contain_text("42")

        # Persisted tab restoration and format-specific controls survive reload.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("activeTab()?.format === 'gspy' && cells.length === 3")
        assert json.loads(scientific.read_text())["metadata"]["gusnotebook"]["document_id"] == document_id
        page.evaluate("path => switchTab(path)", ordinary)
        page.click("#workspace-more")
        expect(page.locator("#document-history-button")).to_be_hidden()
        expect(page.get_by_role("menuitem", name="Change history", exact=True)).to_be_visible()
        assert Path(ordinary).read_bytes() == ordinary_bytes
        assert not errors, errors
        browser.close()
    print("GSPY creation, CLI, ordered execution, history export, restoration, and mixed tabs passed.")


if __name__ == "__main__":
    main()
