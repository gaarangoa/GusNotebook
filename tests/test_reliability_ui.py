"""Offline browser, kernel, save, preview and history checks on a disposable app."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright
from ui_server import rerun_isolated, launch_browser


def check_cli_lifecycle():
    with tempfile.TemporaryDirectory(prefix="gusnb-launch-") as temporary:
        root = Path(temporary)
        state = root / "state"
        env = {**os.environ, "GUSNOTEBOOK_HOME": str(state), "HOST": "127.0.0.1"}
        for key in ("NOTEBOOK", "NB_TOKEN", "GUSNOTEBOOK_TOKEN", "NB_URL", "NB_SESSION", "NB_NOTEBOOK"):
            env.pop(key, None)
        command = [sys.executable, "-m", "gusnotebook", "--port", "0", "--no-browser"]
        with (root / "launch.log").open("w+") as log:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while not list(state.glob("server-*.json")):
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError("CLI launch failed: " + log.read())
                    time.sleep(0.05)
                connection = next(state.glob("server-*.json"))
                metadata = json.loads(connection.read_text())
                assert stat.S_IMODE(connection.stat().st_mode) == 0o600
                cli = subprocess.run([sys.executable, "-m", "gusnotebook.cli", "tabs"],
                    cwd=root, env={**env, "NB_URL": metadata["url"]},
                    capture_output=True, text=True, timeout=30)
                assert cli.returncode == 0 and ".ipynb" in cli.stdout, cli.stderr
                before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
                duplicate = subprocess.run([sys.executable, "-m", "gusnotebook", "--port",
                    str(urllib.parse.urlsplit(metadata["url"]).port), "--no-browser"],
                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
                assert duplicate.returncode != 0
                after = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
                assert before == after, "Failed second launch changed the live app's state"
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            assert process.returncode == 0
            assert not list(state.glob("server-*.json")), "Connection metadata survived shutdown"
        print("PASS: CLI launch, private token discovery, occupied port and shutdown", flush=True)


def main():
    if os.environ.get("GUSNOTEBOOK_ISOLATED_TEST") != "1":
        check_cli_lifecycle()
    rerun_isolated(__file__)
    url, token = os.environ["GUSNOTEBOOK_TEST_URL"], os.environ["NB_TOKEN"]
    root = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"])

    def api(path, body=None, method=None):
        request = urllib.request.Request(url.rstrip("/") + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method or ("POST" if body is not None else "GET"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def offline(route):
            host = urllib.parse.urlsplit(route.request.url).hostname
            if host not in {"127.0.0.1", "localhost"}:
                external.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route("http*://**/*", offline)
        page.goto(url + "#token=" + token)
        page.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM && window.Terminal && window.DOMPurify")
        page.wait_for_selector(".cm-editor")
        assert not external, external
        print("PASS: authenticated startup and browser libraries work offline", flush=True)

        result = page.evaluate("""async () => {
          const id = cells[0].id;
          const originalApi = api;
          // Fail only fetch transport, keeping the real save logic intact.
          window.savedFetch = window.fetch;
          window.fetch = (url, options) => options && options.method === 'PATCH'
            ? Promise.reject(new Error('simulated disconnect')) : window.savedFetch(url, options);
          document.getElementById('ed-' + id).value = 'retained = 42';
          try { await saveCell(id); } catch (_) {}
          return {dirty: activeTab().dirty, pending: unsaved.has(id), id};
        }""")
        assert result["dirty"] and result["pending"], result
        original_session = page.evaluate("currentSession")
        other = api("/api/sessions", {"name": "Other workspace", "switch": False})
        page.evaluate("async id => { await switchSession(id); }", other["id"])
        page.evaluate("async id => { await switchSession(id); }", original_session)
        assert page.evaluate("id => document.getElementById('ed-' + id).value", result["id"]) == "retained = 42"
        assert page.evaluate("activeTab().dirty")
        page.evaluate("window.fetch = window.savedFetch")
        page.evaluate("async id => { await saveCell(id); }", result["id"])
        assert not page.evaluate("activeTab().dirty")
        assert api("/api/notebook")["cells"][0]["source"] == "retained = 42"
        print("PASS: failed save preserves the draft and retry persists it", flush=True)

        edit_id = api("/api/cells", {"cell_type": "code", "source": "first = 1\n"})["id"]
        page.evaluate("load()")
        page.wait_for_function("id => cmViews.has(id)", arg=edit_id)
        page.evaluate("id => { document.getElementById('ed-' + id).value = 'shimmed = 1\\n'; }", edit_id)
        api("/api/cells", {"cell_type": "code", "source": "second = 2\n"})
        page.evaluate("load()")
        page.click(f"#ed-{edit_id} .cm-content")
        page.keyboard.press("End")
        page.keyboard.type("x = 1")
        page.wait_for_function("id => !unsaved.has(id)", arg=edit_id)
        editor_state = page.evaluate("""id => ({depth: CM.undoDepth(cmViews.get(id).state),
          text: cmViews.get(id).state.doc.toString()})""", edit_id)
        assert editor_state["depth"] > 0 and "x = 1" in editor_state["text"], editor_state
        page.evaluate("id => cellUndo(id)", edit_id)
        assert "x = 1" not in page.evaluate("id => cmViews.get(id).state.doc.toString()", edit_id)
        page.evaluate("async () => { await flushNotebook(); }")
        print("PASS: typing and per-cell undo survive saves and added cells", flush=True)

        page.evaluate("""id => {
          foldOpenOnFocus = true;
          document.getElementById('ed-' + id).value = '';
          focusCellEditor(id);
        }""", edit_id)
        for index in range(14):
            page.keyboard.type(f"line_{index} = {index}")
            page.keyboard.press("Enter")
        assert "line_13 = 13" in page.evaluate("id => document.getElementById('ed-' + id).value", edit_id)
        assert page.locator(f"#fold-{edit_id}.folded").count() == 0
        page.keyboard.press("Meta+a" if sys.platform == "darwin" else "Control+a")
        page.keyboard.type("short = 1 # still typing")
        assert page.evaluate("id => document.getElementById('ed-' + id).value", edit_id) == "short = 1 # still typing"
        page.evaluate("async () => { await flushNotebook(); foldOpenOnFocus = false; }")
        print("PASS: growing and shrinking a cell preserves typing focus", flush=True)

        page.evaluate("async id => { await runCell(id); }", result["id"])
        initial = api("/api/tabs")["primary"]
        renamed = api("/api/files/rename", {"path": initial, "name": "renamed.ipynb"})["path"]
        page.wait_for_function("active.endsWith('/renamed.ipynb')")
        output = api("/api/cells/" + result["id"] + "/run?notebook=" + urllib.parse.quote(renamed),
                     {"source": "print(retained)"})
        assert "42" in json.dumps(output), output
        print("PASS: notebook rename retains the live kernel namespace", flush=True)

        visual = root / "work" / "preview.html"
        visual.write_text("<!doctype html><html><body><h1>Original title</h1><button onclick=\"this.textContent='Clicked'\">Click me</button></body></html>")
        page.evaluate("async path => { await openFile(path); }", str(visual))
        frame = page.frame_locator("#html-preview-frame")
        frame.locator("h1").wait_for()
        assert frame.locator("h1").inner_text() == "Original title"
        frame.locator("button").click()
        assert frame.locator("button").inner_text() == "Clicked"
        preview = api("/api/previews")["previews"][0] if "previews" in api("/api/previews") else None
        if preview:
            response = page.request.get(preview["origin"] + "/preview.html")
            assert response.status == 200  # browser's preview capability cookie
        visual.write_text(visual.read_text().replace("Original title", "Refreshed title"))
        from playwright.sync_api import expect
        expect(frame.locator("h1")).to_have_text("Refreshed title")
        visual.write_text(visual.read_text().replace("Refreshed title", "Original title"))
        expect(frame.locator("h1")).to_have_text("Original title")
        print("PASS: authenticated preview loads scripts and reloads external edits", flush=True)

        terminal = api("/api/terminals", {"kind": "shell", "cwd": str(root / "work")})
        transcript = page.evaluate("""id => new Promise((resolve, reject) => {
          const socket = new WebSocket(location.origin.replace(/^http/, 'ws') + '/ws/' + id);
          const timer = setTimeout(() => {socket.close(); reject(new Error('CLI did not reach the app'));}, 15000);
          let output = '';
          socket.onopen = () => socket.send('gusnb tabs\\n');
          socket.onmessage = async event => {
            output += typeof event.data === 'string' ? event.data : await event.data.text();
            if (output.includes('renamed.ipynb')) {
              clearTimeout(timer); socket.close(); resolve(output);
            }
          };
          socket.onerror = () => {clearTimeout(timer); reject(new Error('Terminal authentication failed'));};
        })""", terminal["id"])
        assert "renamed.ipynb" in transcript
        api("/api/terminals/" + terminal["id"], method="DELETE")
        print("PASS: authenticated terminal and embedded CLI reach the same app", flush=True)

        # Both tabs were open when recording began. Direct disk edits are captured.
        group_id = api("/api/history", {"prompt": "Update report and notebook"})["id"]
        visual.write_text(visual.read_text().replace("Original title", "Updated title"))
        api("/api/cells/" + result["id"] + "?notebook=" + urllib.parse.quote(renamed),
            {"source": "print('changed')"}, method="PATCH")
        group = api(f"/api/history/{group_id}/finish", {})
        assert len(group["changes"]) == 2, group
        page.click("#history-button")
        page.get_by_text("Update report and notebook", exact=True).wait_for()
        page.get_by_role("button", name="Undo these changes").click()
        page.get_by_text("Restored ·", exact=False).wait_for()
        assert "Original title" in visual.read_text()
        assert api("/api/notebook?notebook=" + urllib.parse.quote(renamed))["cells"][0]["source"] == "print(retained)"
        page.evaluate("closeHistory()")
        print("PASS: history reviews and restores grouped notebook/file changes", flush=True)

        page.evaluate("path => switchTab(path)", renamed)
        page.wait_for_function("isNotebookTab() && cells.length")
        page.evaluate("""() => {
          window.originalCellNode = document.querySelector('.cell');
          render();
        }""")
        assert page.evaluate("window.originalCellNode === document.querySelector('.cell')")
        assert not external, external
        assert not errors, errors
        print("PASS: unchanged cell DOM is preserved; no browser errors", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
