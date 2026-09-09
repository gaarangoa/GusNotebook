"""Host/client launchers and a real browser through an offline TCP tunnel.

The fake CLI substitutes only Microsoft's account/relay service. HTTP, SSE,
WebSockets, the remote kernel, shell and preview editor are the real app.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import expect, sync_playwright
from ui_server import launch_browser


def wait_log(process, log, pattern):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        text = log.read_text()
        found = re.search(pattern, text)
        if found:
            return found
        if process.poll() is not None:
            raise AssertionError(text)
        time.sleep(.05)
    raise AssertionError("Timed out starting tunnel:\n" + log.read_text())


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    with tempfile.TemporaryDirectory(prefix="gusnb-tunnel-ui-") as temporary:
        root = Path(temporary).resolve()
        remote, local, relay = [root / name for name in ("remote", "local", "relay")]
        for directory in [remote, local, relay]:
            directory.mkdir()
        fake = root / "devtunnel"
        fake.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name("fake_devtunnel.py").read_text())
        fake.chmod(0o755)
        env = {**os.environ, "GUSNOTEBOOK_DEVTUNNEL": str(fake), "GUSNOTEBOOK_FAKE_TUNNEL": str(relay),
               "NO_LLM": "1", "GUSNOTEBOOK_NO_AUTH": "0"}
        for key in ["APP_BASE_URL", "HOST", "PORT", "FLASK_DEBUG", "NOTEBOOK", "GUSNOTEBOOK_TOKEN", "NB_URL", "NB_TOKEN"]:
            env.pop(key, None)
        command = [sys.executable, "-m", "gusnotebook"]
        processes = []
        streams = []

        def start(arguments, directory, state_name, log_name):
            path = root / log_name
            stream = path.open("w")
            streams.append(stream)
            process = subprocess.Popen(command + arguments, cwd=directory,
                env={**env, "GUSNOTEBOOK_HOME": str(root / state_name)}, stdout=stream, stderr=subprocess.STDOUT)
            processes.append(process)
            return process, path

        try:
            host, host_log = start(["--tunnel", "remote-research", "--port", "0", "--no-browser"],
                                   remote, "remote-state", "host.log")
            wait_log(host, host_log, r"GusNotebook — http://")
            client, client_log = start(["--connect", "remote-research", "--no-browser"],
                                       local, "local-state", "client.log")
            url = wait_log(client, client_log, r"Remote GusNotebook — (http://\S+)")[1]
            assert not list(local.iterdir()) and not (root / "local-state").exists()
            calls = [json.loads(line) for line in (relay / "calls.jsonl").read_text().splitlines()]
            assert not any("--allow-anonymous" in call for call in calls)
            assert len(json.loads((relay / "tunnel.json").read_text())["ports"]) == 1
            print("PASS: private host/client workflow forwards one port without creating a local workspace", flush=True)

            with sync_playwright() as playwright:
                browser = launch_browser(playwright)
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM")
                assert not page.locator(".unlock-card").count()
                source = "from pathlib import Path\nremote_value = 41\nPath('remote-result.txt').write_text(str(remote_value))\nprint('REMOTE_KERNEL_OK')"
                page.evaluate("""async source => {
                  await api('/api/cells/' + cells[0].id + nbq(), {method:'PATCH', body:JSON.stringify({source})});
                  await load(); await runCell(cells[0].id);
                }""", source)
                expect(page.locator(".output.stream")).to_contain_text("REMOTE_KERNEL_OK")
                assert (remote / "remote-result.txt").read_text() == "41"
                assert not (local / "remote-result.txt").exists()
                page.evaluate("path => openTerminal(path, 'shell')", str(remote))
                page.wait_for_function("terms.length === 1 && terms[0].ws.readyState === 1")
                page.evaluate("terms[0].ws.send('echo TUNNEL_TERMINAL_OK\\r')")
                expect(page.locator(".term-host.on .xterm-rows")).to_contain_text("TUNNEL_TERMINAL_OK")
                page.evaluate("terms[0].ws.send('gusnb list\\r')")
                expect(page.locator(".term-host.on .xterm-rows")).to_contain_text("remote_value")
                print("PASS: notebook execution, output events, terminal WebSockets and agent CLI reach the remote workspace", flush=True)

                report = remote / "report.html"
                report.write_text('<!doctype html><html><head><link rel="stylesheet" href="/style.css">'
                    '<script type="module" src="/boot.js"></script></head><body><h1>Remote report</h1>'
                    '<p id="data">Loading</p><button onclick="this.textContent=\'Clicked\'">Click me</button></body></html>')
                (remote / "style.css").write_text("h1 {color: rgb(120, 30, 80)}")
                (remote / "data.json").write_text('{"value":"REMOTE_ASSET_OK"}')
                (remote / "boot.js").write_text("const data = await fetch('/data.json').then(r => r.json()); document.getElementById('data').textContent = data.value;")
                page.evaluate("path => openFile(path)", str(report))
                frame = page.frame_locator("#html-preview-frame")
                expect(frame.locator("#data")).to_have_text("REMOTE_ASSET_OK")
                expect(frame.locator("h1")).to_have_css("color", "rgb(120, 30, 80)")
                frame.get_by_role("button", name="Click me").click()
                expect(frame.get_by_role("button", name="Clicked")).to_be_visible()
                frame.locator("h1").fill("Edited through the tunnel")
                page.wait_for_function("activeTab().dirty")
                page.evaluate("saveText()")
                page.wait_for_function("!activeTab().dirty")
                assert "Edited through the tunnel" in report.read_text()
                assert not (local / "report.html").exists()
                preview_url = page.locator("#html-preview-frame").get_attribute("src")
                assert re.search(r":(\d+)/", preview_url)[1] == re.search(r":(\d+)/", url)[1]
                blocked = frame.locator("body").evaluate("""async (_body, origin) => {
                  try { await fetch(origin + '/api/notebook', {credentials:'include'}); return false; }
                  catch (_) { return true; }
                }""", url.rstrip("/"))
                assert blocked
                print("PASS: isolated HTML editing, modules, root-relative assets and fetch work through the same port", flush=True)

                # Disconnect the client only, then attach a second local launcher.
                term_id = page.evaluate("terms[0].id")
                stop(client)
                assert client.returncode == 0 and host.poll() is None
                replacement, replacement_log = start(["--connect", "remote-research", "--no-browser"],
                                                     local, "local-state", "reconnect.log")
                reconnect_url = wait_log(replacement, replacement_log, r"Remote GusNotebook — (http://\S+)")[1]
                page.goto(reconnect_url, wait_until="domcontentloaded")
                page.wait_for_function("typeof terms !== 'undefined' && terms.length === 1 && terms[0].ws.readyState === 1")
                assert page.evaluate("terms[0].id") == term_id
                notebook_path = str(next(remote.glob("*.ipynb")))
                page.evaluate("path => openFile(path)", notebook_path)
                page.wait_for_function("activeTab().kind === 'notebook'")
                page.evaluate("""async () => {
                  const cell = await api('/api/cells' + nbq(), {method:'POST', body:JSON.stringify({source:'print(remote_value + 1)'})});
                  await load(); await runCell(cell.id);
                }""")
                expect(page.locator(".output.stream").last).to_contain_text("42")
                assert not list(local.iterdir()) and not (root / "local-state").exists()
                assert not errors, errors
                page.screenshot(path=str(Path(tempfile.gettempdir()) / "gusnb-tunnel.png"))
                browser.close()
                print("PASS: local disconnect preserves remote kernel variables, terminals and files for reconnect", flush=True)
            stop(host)
            assert host.returncode == 0
            assert json.loads((relay / "tunnel.json").read_text())["hostConnections"] == 0
            assert not list((root / "remote-state").glob("server-*.json"))
        finally:
            for process in reversed(processes):
                stop(process)
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    main()
