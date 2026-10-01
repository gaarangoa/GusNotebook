"""Local picker -> separate remote workspace through a real TCP forward.

Only Microsoft's account/relay service is substituted. Kernels, terminal
WebSockets, auth, the registry, CLI processes, and both apps are real.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_tunnels_ui import stop, wait_log
from ui_server import launch_browser


def main():
    with tempfile.TemporaryDirectory(prefix="gusnb-picker-ui-") as temporary:
        root = Path(temporary).resolve()
        remote, local, relay = [root / name for name in ("remote", "local", "relay")]
        for directory in (remote, local, relay):
            directory.mkdir()
        fake = root / "devtunnel"
        fake.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name("fake_devtunnel.py").read_text())
        fake.chmod(0o755)
        gh = root / "gh"
        gh.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name("fake_gh.py").read_text())
        gh.chmod(0o755)
        gh_account = root / "github"
        gh_account.mkdir()
        (gh_account / "account.json").write_text(json.dumps({"login": "test-user", "token": "gho_TEST_ONLY_NEVER_VALID"}))
        env = {**os.environ, "GUSNOTEBOOK_DEVTUNNEL": str(fake), "GUSNOTEBOOK_FAKE_TUNNEL": str(relay),
               "GUSNOTEBOOK_GH": str(gh), "GUSNOTEBOOK_FAKE_GH": str(gh_account), "GH_TOKEN": "", "GITHUB_TOKEN": "",
               "NO_LLM": "1", "GUSNOTEBOOK_NO_AUTH": "0"}
        for key in ["APP_BASE_URL", "HOST", "PORT", "FLASK_DEBUG", "NOTEBOOK", "GUSNOTEBOOK_TOKEN", "NB_URL", "NB_TOKEN"]:
            env.pop(key, None)
        processes, streams = [], []

        def start(arguments, directory, state, log_name):
            log = root / log_name
            stream = log.open("w")
            streams.append(stream)
            process = subprocess.Popen([sys.executable, "-m", "gusnotebook", *arguments], cwd=directory,
                env={**env, "GUSNOTEBOOK_HOME": str(root / state)}, stdout=stream, stderr=subprocess.STDOUT)
            processes.append(process)
            return process, wait_log(process, log, r"GusNotebook — (http://\S+)")[1]

        def calls(command):
            return [args for args in map(json.loads, (relay / "calls.jsonl").read_text().splitlines()) if args[0] == command]

        try:
            host, _ = start(["--tunnel", "remote-research", "--port", "0", "--no-browser"], remote, "remote-state", "host.log")
            (relay / "account.json").write_text('{"status":"Logged out"}')
            (relay / "device-login").touch()
            client, url = start(["--port", "0", "--no-browser"], local, "local-state", "local.log")
            with sync_playwright() as playwright:
                browser = launch_browser(playwright)
                context = browser.new_context(viewport={"width": 1440, "height": 960})
                errors = []
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                page = context.new_page()

                def open_local(address):
                    page.goto(address, wait_until="domcontentloaded")
                    page.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM")
                    page.locator("#sidebar-tunnels").click()
                    expect(page.locator("#tunnels")).to_be_visible()

                def row(name):
                    return page.locator(".tunnel-row").filter(has=page.get_by_role("button", name=re.compile("^" + re.escape(name) + r"\s*(?:Available|Connected|Connecting|Offline|Error|Not checked)$")))

                def menu(name, action):
                    row(name).get_by_role("button", name="Actions for " + name, exact=True).click()
                    page.get_by_role("menuitem", name=action, exact=True).click()

                def launch(name):
                    with context.expect_page() as opened:
                        row(name).locator(".tunnel-open").click()
                    popup = opened.value
                    popup.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM")
                    return popup

                open_local(url)
                page.locator("#accounts-button").click()
                expect(page.locator("#tunnel-account")).to_have_text("Tunnels · Not signed in")
                page.get_by_role("button", name="Sign in with GitHub").click()
                expect(page.locator("#tunnel-login-output")).to_contain_text("TEST-1234")
                expect(page.get_by_role("link", name="Open sign-in page")).to_have_attribute("href", "https://github.com/login/device")
                page.get_by_role("button", name="Cancel sign-in").click()
                expect(page.locator("#tunnel-login-output")).to_be_hidden()
                page.locator(".account-alternative summary").click()
                page.get_by_role("button", name="Use Microsoft for tunnels").click()
                expect(page.get_by_role("link", name="Open sign-in page")).to_have_attribute("href", "https://microsoft.com/devicelogin")
                (relay / "login-ready").touch()
                expect(page.locator("#tunnel-account")).to_have_text("Tunnels · Microsoft · test-user")
                page.keyboard.press("Escape")
                expect(row("remote-research")).to_contain_text("Available")
                print("PASS: device-code instructions, cancellation, account sign-in and discovery", flush=True)

                remote_page = launch("remote-research")
                expect(remote_page.locator(".remote-badge")).to_have_text("Remote: remote-research")
                assert not remote_page.locator("#sidebar-tunnels").count()
                expect(row("remote-research")).to_contain_text("Connected")
                assert remote_page.url.startswith("http://gusnotebook.localhost:")
                assert "#token=" not in remote_page.url
                assert not remote_page.locator(".unlock-card").count()
                again = launch("remote-research")
                assert again.url == remote_page.url
                assert len(calls("connect")) == 1
                again.close()

                source = "from pathlib import Path\nremote_value = 41\nPath('remote-result.txt').write_text(str(remote_value))\nprint('REMOTE_KERNEL_OK')"
                remote_page.evaluate("""async source => {
                  await api('/api/cells/' + cells[0].id + nbq(), {method:'PATCH', body:JSON.stringify({source})});
                  await load(); await runCell(cells[0].id);
                }""", source)
                expect(remote_page.locator(".output.stream")).to_contain_text("REMOTE_KERNEL_OK")
                assert (remote / "remote-result.txt").read_text() == "41"
                assert not (local / "remote-result.txt").exists()
                remote_page.evaluate("path => openTerminal(path, 'shell')", str(remote))
                remote_page.wait_for_function("terms.length === 1 && terms[0].ws.readyState === 1")
                remote_page.evaluate("terms[0].ws.send('echo TUNNEL_TERMINAL_OK\\r')")
                expect(remote_page.locator(".term-host.on .xterm-rows")).to_contain_text("TUNNEL_TERMINAL_OK")
                term_id = remote_page.evaluate("terms[0].id")
                print("PASS: click opens remote window; repeat clicks reuse one forward; files and terminals run remotely", flush=True)

                menu("remote-research", "Rename…")
                page.locator("#ask-input").fill("Research workstation")
                page.locator("#ask-ok").click()
                expect(row("Research workstation")).to_contain_text("Connected")
                menu("Research workstation", "Details")
                expect(page.locator("#tunnel-id")).to_have_value("remote-research")
                expect(page.locator("#tunnel-details")).to_contain_text("Last connected:")
                page.keyboard.press("Escape")
                for theme in ("light", "dark"):
                    page.evaluate("theme => AppAppearance.update({theme})", theme)
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    page.screenshot(path=str(Path(tempfile.gettempdir()) / f"gusnb-tunnels-{theme}.png"))
                page.set_viewport_size({"width": 700, "height": 900})
                page.locator("#sidebar-tunnels").click()
                expect(page.locator("#tunnels")).to_be_visible()
                expect(row("Research workstation")).to_be_visible()
                page.set_viewport_size({"width": 1440, "height": 960})

                menu("Research workstation", "Disconnect")
                expect(row("Research workstation")).to_contain_text("Available")
                assert host.poll() is None
                remote_page.close()
                (relay / "hold-connect").touch()
                with context.expect_page() as opened:
                    row("Research workstation").locator(".tunnel-open").click()
                canceled = opened.value
                expect(row("Research workstation")).to_contain_text("Connecting")
                canceled.get_by_role("button", name="Cancel connection").click()
                expect(canceled.locator("#connection-status")).to_contain_text("Connection canceled")
                expect(row("Research workstation")).to_contain_text("Available")
                (relay / "hold-connect").unlink()
                canceled.close()
                remote_page = launch("Research workstation")
                remote_page.wait_for_function("terms.length === 1 && terms[0].ws.readyState === 1")
                assert remote_page.evaluate("terms[0].id") == term_id
                print("PASS: rename, details, both themes, narrow layout, cancel and reconnect", flush=True)

                stop(client)
                assert client.returncode == 0 and host.poll() is None
                remote_page.close()
                client, url = start(["--port", "0", "--no-browser"], local, "local-state", "local-restart.log")
                open_local(url)
                expect(row("Research workstation")).to_be_visible()
                remote_page = launch("Research workstation")
                remote_page.wait_for_function("terms.length === 1 && terms[0].ws.readyState === 1")
                assert remote_page.evaluate("terms[0].id") == term_id
                remote_page.evaluate("""async () => {
                  const cell = await api('/api/cells' + nbq(), {method:'POST', body:JSON.stringify({source:'print(remote_value + 1)'})});
                  await load(); await runCell(cell.id);
                }""")
                expect(remote_page.locator(".output.stream").last).to_contain_text("42")
                print("PASS: restarting local app preserves saved names and remote kernel variables and terminal", flush=True)

                (relay / "fail-list").touch()
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.locator("#tunnel-error")).to_contain_text("Relay service unavailable")
                expect(row("Research workstation")).to_contain_text("Connected")
                (relay / "fail-list").unlink()
                page.get_by_role("button", name="Add tunnel", exact=True).click()
                page.locator("#tunnel-name").fill("Missing workstation")
                page.locator("#tunnel-id").fill("missing-tunnel")
                page.locator("#tunnel-save").click()
                with context.expect_page() as opened:
                    row("Missing workstation").locator(".tunnel-open").click()
                missing = opened.value
                expect(missing.locator("#connection-status")).to_contain_text("Tunnel not found")
                expect(missing.get_by_role("button", name="Retry", exact=True)).to_be_visible()
                missing.close()
                menu("Missing workstation", "Remove from list")
                page.locator("#ask-ok").click()
                expect(row("Missing workstation")).to_have_count(0)
                menu("Research workstation", "Remove from list")
                page.locator("#ask-ok").click()
                expect(page.get_by_role("button", name="Actions for Research workstation")).to_have_count(0)
                assert host.poll() is None
                assert json.loads((root / "local-state" / "tunnels.json").read_text()) == []
                assert not errors, errors
                browser.close()
                print("PASS: cloud failures and missing IDs are actionable; removing saves never deletes or stops the remote", flush=True)
        finally:
            for process in reversed(processes):
                stop(process)
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    main()
