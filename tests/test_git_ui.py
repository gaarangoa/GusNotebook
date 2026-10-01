"""Git panel and credential sharing across real local/remote apps and PTYs."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_tunnels_ui import stop, wait_log
from ui_server import launch_browser


def main():
    with tempfile.TemporaryDirectory(prefix="gusnb-git-ui-") as temporary:
        root = Path(temporary).resolve()
        local, remote, relay, local_auth, remote_auth = [root / name for name in ("local", "remote", "relay", "local-auth", "remote-auth")]
        for directory in (local, remote, relay, local_auth, remote_auth):
            directory.mkdir()
        config = root / "gitconfig"
        config.write_text('[user]\nname = Git UI Test\nemail = test@example.invalid\n')
        for filename, fixture in [("devtunnel", "fake_devtunnel.py"), ("gh", "fake_gh.py")]:
            executable = root / filename
            executable.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name(fixture).read_text())
            executable.chmod(0o755)
        # Report only a boolean marker, never the credential in stdout/logs.
        probe = root / "probe.py"
        probe.write_text("import subprocess\np = subprocess.run(['git','credential','fill'], input='protocol=https\\nhost=github.com\\n\\n', text=True, capture_output=True)\nprint('GIT_AUTH_OK' if 'gho_TEST_ONLY_NEVER_VALID' in p.stdout else 'GIT_AUTH_MISSING', flush=True)\n")
        fake_agent = root / "claude"
        fake_agent.write_text("#!" + sys.executable + "\n" + probe.read_text() + "import time; time.sleep(10)\n")
        fake_agent.chmod(0o755)
        fake_codex = root / "codex"
        fake_codex.write_text(fake_agent.read_text())
        fake_codex.chmod(0o755)
        fake_uv = root / "uv"
        fake_uv.write_text("#!/bin/sh\nexit 0\n")
        fake_uv.chmod(0o755)
        env = {**os.environ, "GUSNOTEBOOK_DEVTUNNEL": str(root / "devtunnel"), "GUSNOTEBOOK_FAKE_TUNNEL": str(relay),
            "GUSNOTEBOOK_GH": str(root / "gh"), "GUSNOTEBOOK_UV": str(fake_uv), "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": "", "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false",
            "GH_TOKEN": "", "GITHUB_TOKEN": "", "NO_LLM": "1", "GUSNOTEBOOK_NO_AUTH": "0",
            "PATH": str(root) + os.pathsep + os.environ["PATH"], "ZDOTDIR": str(root)}
        for key in ["APP_BASE_URL", "HOST", "PORT", "FLASK_DEBUG", "NOTEBOOK", "GUSNOTEBOOK_TOKEN", "NB_URL", "NB_TOKEN"]:
            env.pop(key, None)
        for repo in (local, remote):
            subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
            (repo / ".gitignore").write_text("*.ipynb\n")
            subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], env=env, check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Initial"], env=env, check=True)
        processes, streams = [], []
        def start(arguments, work, auth, label):
            log = root / (label + ".log")
            stream = log.open("w")
            streams.append(stream)
            process = subprocess.Popen([sys.executable, "-m", "gusnotebook", *arguments], cwd=work,
                env={**env, "GUSNOTEBOOK_HOME": str(root / (label + "-state")), "GUSNOTEBOOK_FAKE_GH": str(auth)},
                stdout=stream, stderr=subprocess.STDOUT)
            processes.append(process)
            return process, wait_log(process, log, r"GusNotebook — (http://\S+)")[1]
        try:
            (relay / "account.json").write_text(json.dumps({"status": "Logged in", "provider": "GitHub", "username": "researcher"}))
            host, _ = start(["--tunnel", "git-workstation", "--port", "0", "--no-browser"], remote, remote_auth, "remote")
            client, url = start(["--port", "0", "--no-browser"], local, local_auth, "local")
            with sync_playwright() as playwright:
                browser = launch_browser(playwright)
                context = browser.new_context(viewport={"width": 1440, "height": 960})
                errors = []
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM")
                page.locator("#sidebar-git").click()
                expect(page.locator("#git-changes")).to_contain_text("Working tree clean")
                (local / "analysis.py").write_text("print('first version')\n")
                expect(page.locator(".git-file")).to_have_text("analysis.py")
                page.locator(".git-file").click()
                expect(page.locator("#git-diff-text")).to_contain_text("first version")
                page.keyboard.press("Escape")
                page.get_by_role("button", name="Stage analysis.py", exact=True).click()
                expect(page.get_by_role("button", name="Unstage analysis.py", exact=True)).to_be_visible()
                page.locator("#git-message").fill("Add analysis")
                page.locator("#git-commit-button").click()
                expect(page.locator("#git-changes")).to_contain_text("Working tree clean")
                expect(page.locator("#git-message")).to_have_value("")
                assert subprocess.run(["git", "-C", str(local), "log", "-1", "--format=%s"], env=env, capture_output=True, text=True).stdout.strip() == "Add analysis"
                print("PASS: Git sidebar detects edits, renders diff, stages and commits", flush=True)

                page.locator("#accounts-button").click()
                page.get_by_role("button", name="Sign in with GitHub", exact=True).click()
                expect(page.locator("#git-login-output")).to_contain_text("TEST-2468")
                expect(page.locator("#git-login-link")).to_be_visible()
                page.get_by_role("button", name="Cancel sign-in", exact=True).click()
                expect(page.locator("#git-login-output")).to_be_hidden()
                page.get_by_role("button", name="Sign in with GitHub", exact=True).click()
                expect(page.locator("#git-login-output")).to_contain_text("TEST-2468")
                (local_auth / "login-ready").touch()
                expect(page.locator("#git-account")).to_have_text("GitHub · researcher")
                assert not (remote_auth / "account.json").exists()
                for theme in ("light", "dark"):
                    page.evaluate("theme => AppAppearance.update({theme})", theme)
                    page.screenshot(path=str(Path(tempfile.gettempdir()) / f"gusnb-git-{theme}.png"))
                page.evaluate("path => openTerminal(path, 'shell')", str(local))
                page.wait_for_function("terms.length === 1 && terms[0].ws.readyState === 1")
                command = shlex.join([sys.executable, str(probe)]) + "\r"
                page.evaluate("command => terms[0].ws.send(command)", command)
                expect(page.locator(".term-host.on .xterm-rows")).to_contain_text("GIT_AUTH_OK")
                print("PASS: device-code login/cancel and reuse by a local terminal", flush=True)

                page.locator("#sidebar-tunnels").click()
                expect(page.locator(".tunnel-open")).to_contain_text("git-workstation")
                with context.expect_page() as popup:
                    page.locator(".tunnel-open").click()
                remote_page = popup.value
                remote_page.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM")
                remote_page.locator("#sidebar-git").click()
                remote_page.locator("#accounts-button").click()
                expect(remote_page.locator("#git-account")).to_contain_text("shared from your local app")
                remote_page.keyboard.press("Escape")
                remote_page.evaluate("path => openTerminal(path, 'shell')", str(remote))
                remote_page.wait_for_function("terms.length === 1 && terms[0].ws.readyState === 1")
                remote_page.evaluate("command => terms[0].ws.send(command)", command)
                expect(remote_page.locator(".term-host.on .xterm-rows")).to_contain_text("GIT_AUTH_OK", timeout=15000)
                remote_page.evaluate("path => openTerminal(path, 'codex')", str(remote))
                remote_page.wait_for_function("terms.length === 2")
                expect(remote_page.locator(".term-host.on .xterm-rows")).to_contain_text("GIT_AUTH_OK", timeout=15000)
                remote_page.evaluate("path => openTerminal(path, 'claude')", str(remote))
                remote_page.wait_for_function("terms.length === 3")
                expect(remote_page.locator(".term-host.on .xterm-rows")).to_contain_text("GIT_AUTH_OK", timeout=15000)
                assert not (remote_auth / "account.json").exists()
                assert not (remote / ".git-credentials").exists()
                assert "gho_TEST_ONLY_NEVER_VALID" not in json.dumps(remote_page.evaluate("api('/api/git/auth')"))
                print("PASS: one local sign-in works in remote terminal and agent through the tunnel", flush=True)

                (remote / "remote-analysis.py").write_text("print('remote change')\n")
                expect(remote_page.locator(".git-file")).to_have_text("remote-analysis.py")
                remote_page.get_by_role("button", name="Stage remote-analysis.py", exact=True).click()
                expect(remote_page.locator("#git-commit-button")).to_be_enabled()
                remote_page.locator("#git-message").fill("Remote change")
                remote_page.locator("#git-commit-button").click()
                expect(remote_page.locator("#git-changes")).to_contain_text("Working tree clean")
                assert not (local / "remote-analysis.py").exists()
                assert not errors, errors
                # No credentials in app logs, saved state or visible terminal output.
                for label in ("local", "remote"):
                    assert "gho_TEST_ONLY_NEVER_VALID" not in (root / (label + ".log")).read_text()
                    for file in (root / (label + "-state")).rglob("*.json"):
                        assert "gho_TEST_ONLY_NEVER_VALID" not in file.read_text()
                browser.close()
                print("PASS: remote Git panel changes only the remote repository; no tokens in UI, logs or app state", flush=True)
            stop(client)
            assert client.returncode == 0 and host.poll() is None
        finally:
            for process in reversed(processes):
                stop(process)
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    main()
