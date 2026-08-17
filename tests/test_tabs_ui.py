"""End-to-end check of tabs, per-notebook kernels, and the env picker.

Drives the running app in Chrome. Start the app first (`uv run gusnotebook`),
then:

    uv run python tests/test_tabs_ui.py

Expects the fixtures in /tmp/nbtest (see make_fixtures below) and a second
virtualenv at /tmp/venv312 with ipykernel installed.
"""

import base64
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

from playwright.sync_api import sync_playwright

URL = os.environ.get("GUSNOTEBOOK_TEST_URL", "http://localhost:8888/")
FIX = pathlib.Path("/tmp/nbtest")
ALT_VENV = pathlib.Path("/tmp/venv312")


def api(route, method="GET", body=None):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        URL.rstrip("/") + route, method=method, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def close_stray_tabs():
    """Leave the current session holding exactly the primary notebook.

    The app keeps documents open server-side, so tabs from an earlier run — or
    from the other suite — would otherwise be counted here as ours.

    Reopening `primary` afterwards is the part that isn't obvious: it is the
    launch notebook, not necessarily a *member* of the session on screen. A
    session the user created and browsed elsewhere holds its own tabs and none
    of them is primary, so closing the rest left the page with no tabs at all.
    """
    tabs = api("/api/tabs")
    for t in tabs["tabs"]:
        if t["path"] != tabs["primary"]:
            api("/api/close", "POST", {"path": t["path"]})
    api("/api/open", "POST", {"path": tabs["primary"]})


def make_fixtures():
    """Recreate the scratch tree; /tmp gets swept, so never assume it's there."""
    FIX.mkdir(exist_ok=True)
    (FIX / "utils.py").write_text('def shout(s):\n    return s.upper()\n')
    (FIX / "data.csv").write_text("site,n\nA,12\nB,7\n")
    (FIX / "huge.bin").write_bytes(bytes(range(256)) * 12000)
    (FIX / "big.txt").write_text(
        "".join(f"row {i} lorem ipsum dolor sit amet\n" for i in range(80000)))
    (FIX / "pic.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKklEQVR42mP8z8DAwMDAx"
        "MDAwMDAxMDAwMDAxMDAwMDAxMDAwMDAxAAAKvgD/Xr0kV8AAAAASUVORK5CYII="))
    for name in ("one.ipynb", "two.ipynb"):
        p = FIX / name
        if not p.exists():
            p.write_text(json.dumps({
                "cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    if not (ALT_VENV / "bin/python3").exists():
        subprocess.run([sys.executable, "-m", "venv", str(ALT_VENV)], check=True)
        subprocess.run([str(ALT_VENV / "bin/pip"), "-q", "install", "ipykernel"],
                       check=True)


checks = []


def check(label, got, want):
    ok = got == want if not callable(want) else want(got)
    checks.append((ok, label, got))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")


def tabnames(pg):
    return pg.evaluate("tabs.map(t => t.name)")


def run_cell(pg, source):
    """Put `source` in the last cell of the active notebook and run it."""
    cid = pg.evaluate("cells[cells.length - 1].id")
    pg.evaluate(f"""(async () => {{
      document.getElementById('ed-{cid}').value = {json.dumps(source)};
      await runCell('{cid}');
    }})()""")
    pg.wait_for_selector(f"#out-{cid} .outputs", timeout=60000)
    pg.wait_for_function(
        f"!document.querySelector('#out-{cid} .spin')", timeout=60000)
    return pg.locator(f"#out-{cid}").inner_text()


def main():
    make_fixtures()
    close_stray_tabs()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(URL, wait_until="domcontentloaded")
        pg.wait_for_function("tabs.length > 0", timeout=20000)

        print("\n-- boot")
        check("one tab open", pg.evaluate("tabs.length"), 1)
        check("it is a notebook", pg.evaluate("tabs[0].kind"), "notebook")
        check("its python is known", pg.evaluate("!!tabs[0].python"), True)

        print("\n-- opening files")
        pg.evaluate(f"browse('{FIX}')")
        pg.wait_for_function("fileState.path && !!document.querySelector('#file-list .file-row')")
        for f in ["one.ipynb", "two.ipynb", "utils.py", "data.csv"]:
            pg.evaluate(f"openFile('{FIX}/{f}')")
            pg.wait_for_function(f"!!tab(tabs.map(t=>t.path).find(p=>p.endsWith('{f}')))")
        check("five tabs", len(tabnames(pg)), 5)
        check("csv is active", pg.evaluate("activeTab().name"), "data.csv")
        check("text pane shown", pg.locator("#textpane").is_visible(), True)
        check("toolbar hidden for text", pg.locator("#toolbar").is_visible(), False)
        check("csv content loaded", pg.input_value("#text-editor").startswith("site,n"), True)

        print("\n-- editing and saving text")
        pg.click("#text-editor")
        pg.keyboard.press("Meta+ArrowDown")
        pg.keyboard.type("C,3\n")
        check("dirty marker", pg.locator("#tabs .tab.dirty").count(), 1)
        pg.keyboard.press("Meta+s")
        pg.wait_for_function("!activeTab().dirty", timeout=15000)
        check("saved to disk", "C,3" in (FIX / "data.csv").read_text(), True)
        check("marker cleared", pg.locator("#tabs .tab.dirty").count(), 0)

        print("\n-- two notebooks, two kernels")
        pg.evaluate(f"switchTab('{FIX.resolve()}/one.ipynb')")
        pg.wait_for_function("activeTab().name === 'one.ipynb'")
        check("toolbar back", pg.locator("#toolbar").is_visible(), True)
        out = run_cell(pg, "secret = 'ONE'\nimport sys; print(sys.executable)")
        check("one ran on .venv", "AI30-CRM/.venv" in out, True)

        pg.evaluate(f"switchTab('{FIX.resolve()}/two.ipynb')")
        pg.wait_for_function("activeTab().name === 'two.ipynb'")
        out = run_cell(pg, "try:\n    print(secret)\nexcept NameError:\n    print('separate')")
        check("namespaces isolated", "separate" in out, True)

        print("\n-- toolbar is down to the essentials")
        labels = [t.strip() for t in pg.locator("#toolbar button.tb").all_inner_texts()]
        # + Raw / + AI joined the four cell types after this check was written,
        # and ⚙ moved to the title bar; the point of it is that nothing *else*
        # crept back in.
        check("nothing but cell types and kernel controls", labels,
              ["+ Code", "+ Markdown", "+ Raw", "+ AI",
               "Delete", "■ Stop", "↻ Restart"])
        check("kernel badge is the env button",
              pg.evaluate("document.getElementById('venv-btn').classList.contains('kernel-badge')"),
              True)
        check("badge shows status", pg.locator("#k-status").inner_text() in
              ("idle", "busy", "starting", "stopped", "dead"), True)
        check("badge shows the env", pg.locator("#venv-name").inner_text() != "", True)

        print("\n-- stop interrupts an active cell")
        stop_cell = api("/api/cells" +
                        "?notebook=" + urllib.parse.quote(pg.evaluate("active")),
                        "POST", {"cell_type": "code", "source":
                                 "import time\ntime.sleep(30)\nprint('not reached')"})["id"]
        pg.evaluate("load()")
        pg.wait_for_selector(f"#ed-{stop_cell}", timeout=20000)
        pg.evaluate(f"setTimeout(() => runCell({stop_cell!r}), 0)")
        pg.wait_for_selector(f"#out-{stop_cell} .spin", timeout=10000)
        pg.click("#kernel-stop")
        pg.wait_for_function("document.getElementById('k-status').textContent === 'stopping'",
                             timeout=5000)
        pg.wait_for_function(f"!document.querySelector('#out-{stop_cell} .spin')",
                             timeout=10000)
        check("Stop ends the run before its sleep completes",
              pg.locator(f"#out-{stop_cell} .output.stream").count(), 0)
        check("the interrupted cell reports KeyboardInterrupt",
              "KeyboardInterrupt" in
              pg.locator(f"#out-{stop_cell} .output.error").inner_text(), True)
        check("kernel returns to idle", pg.locator("#k-status").inner_text(), "idle")

        print("\n-- shortcuts replacing the removed buttons")
        n_before = pg.evaluate("cells.length")
        first = pg.evaluate("cells[0].id")
        pg.click(f"#ed-{first}")
        pg.keyboard.press("Shift+Meta+m")
        pg.wait_for_function(
            f"(cells.find(c => c.id === '{first}') || {{}}).cell_type === 'markdown'",
            timeout=15000)
        check("⇧⌘M made it markdown",
              pg.evaluate(f"cells.find(c => c.id === '{first}').cell_type"), "markdown")
        pg.evaluate(f"changeType('{first}', 'code')")
        pg.wait_for_function(
            f"(cells.find(c => c.id === '{first}') || {{}}).cell_type === 'code'",
            timeout=15000)
        check("cell count unchanged by type toggles", pg.evaluate("cells.length"), n_before)

        print("\n-- env picker")
        pg.click("#venv-btn")
        pg.wait_for_selector("#venv-menu .venv-item", timeout=60000)
        items = [i.replace("\n", " ") for i in
                 pg.locator("#venv-menu .venv-item").all_inner_texts()]
        print("     menu:", items)
        check("has a Browse… row", any("Browse" in i for i in items), True)
        check("marks the current env", pg.locator("#venv-menu .venv-item.current").count(), 1)
        check("run-all moved into the menu", any("Run all" in i for i in items), True)
        check("clear moved into the menu", any("Clear all" in i for i in items), True)
        pg.click("#notebook")
        check("closes on outside click", pg.locator("#venv-menu").is_visible(), False)

        pg.evaluate("setTimeout(() => openDirPicker('/tmp'), 0)")
        pg.wait_for_selector("#dirpick-back.on .dp-venv", timeout=20000)
        check("an arbitrarily named environment is recognized",
              pg.locator("#dirpick-list .dp-venv", has_text="venv312").count(), 1)
        pg.evaluate("dirPickCancel()")

        # A pasted bin directory is normalized to its Python executable too.
        pg.evaluate(f"setVenv('{ALT_VENV / 'bin'}')")
        pg.wait_for_function(
            f"(activeTab().python || '').includes('venv312')", timeout=90000)
        check("badge shows new env", pg.locator("#venv-name").inner_text(), "venv312")
        out = run_cell(pg, "import sys; print(sys.executable)")
        check("two now runs on venv312", "venv312" in out, True)
        check("persisted in the .ipynb",
              "venv312" in (FIX / "two.ipynb").read_text(), True)

        pg.evaluate(f"switchTab('{FIX.resolve()}/one.ipynb')")
        pg.wait_for_function("activeTab().name === 'one.ipynb'")
        out = run_cell(pg, "import sys; print(sys.executable, secret)")
        check("one untouched, state intact", "ONE" in out and "AI30-CRM" in out, True)

        print("\n-- bad env is reported, not silently applied")
        pg.evaluate("setVenv('/tmp/definitely-not-a-venv')")
        pg.wait_for_selector("#flash.on", timeout=20000)
        check("readable error", "no python found" in pg.locator("#flash").inner_text(), True)
        check("env unchanged", "AI30-CRM" in pg.evaluate("activeTab().python"), True)

        print("\n-- non-text files")
        for name, expect in [("huge.bin", "binary"), ("big.txt", "too large")]:
            pg.evaluate(f"openFile('{FIX}/{name}')")
            pg.wait_for_selector("#flash.on", timeout=20000)
            check(f"{name} refused", expect in pg.locator("#flash").inner_text(), True)
        check("no tab was added", len(tabnames(pg)), 5)

        pg.evaluate(f"openFile('{FIX}/pic.png')")
        pg.wait_for_function("activeTab().kind === 'image'", timeout=20000)
        pg.wait_for_function("document.getElementById('imgview').naturalWidth > 0")
        check("image renders", pg.evaluate("document.getElementById('imgview').naturalWidth"), 16)

        print("\n-- reload restores the tab bar")
        before = sorted(tabnames(pg))
        pg.reload(wait_until="domcontentloaded")
        # boot() opens tabs one at a time, so a count threshold is satisfied
        # part-way through the list. Wait for boot to actually finish.
        pg.wait_for_function("booted", timeout=30000)
        check("notebooks and text came back",
              set(t for t in tabnames(pg) if not t.endswith('.png')),
              set(t for t in before if not t.endswith('.png')))

        print("\n-- closing tabs")
        n = pg.evaluate("tabs.length")
        pg.evaluate(f"closeTab('{FIX.resolve()}/two.ipynb')")
        pg.wait_for_function(f"tabs.length === {n - 1}", timeout=20000)
        check("tab removed", pg.evaluate("tabs.some(t => t.name === 'two.ipynb')"), False)
        check("an active tab remains", pg.evaluate("!!active"), True)

        print("\n-- layout survived it all")
        cols = pg.evaluate("getComputedStyle(document.getElementById('app')).gridTemplateColumns")
        check("four columns", len(cols.split()), 4)
        check("no page errors", errors, [])
        b.close()

    bad = [c for c in checks if not c[0]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} passed")
    if bad:
        for _, label, got in bad:
            print(f"  FAILED {label} -> {got!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
