"""End-to-end check of file creation, Claude terminals, and the +AI cell.

Drives the running app in Chrome. Start the app first (`uv run gusnotebook`),
then:

    uv run python tests/test_new_ui.py

The inline-LLM generation test makes a real gateway call; skip it with
NO_LLM=1 if you're offline or don't want to spend the tokens.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

URL = os.environ.get("GUSNOTEBOOK_TEST_URL", "http://localhost:8888/")
FIX = pathlib.Path("/tmp/nbcreate")
# The app's own directory: a real one with dotfiles (.env, .gitignore) in it.
APP_DIR = pathlib.Path(__file__).resolve().parent.parent

# The settings file the *running app* is using. Asked for rather than assumed:
# state moved out of the source tree when this became a package, so a path
# relative to the suite's cwd would check a file nothing writes to — a test that
# passes by reading a stale copy is worse than no test.
SETTINGS_FILE = None


def settings_file():
    global SETTINGS_FILE
    if SETTINGS_FILE is None:
        SETTINGS_FILE = pathlib.Path(get("/api/settings")["settings_path"])
    return SETTINGS_FILE
DEEP = FIX / "one" / "two" / "three" / "four" / "five"

checks = []


def check(label, got, want):
    ok = want(got) if callable(want) else got == want
    checks.append((ok, label, got))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")


def post(route, body=None):
    req = urllib.request.Request(
        URL.rstrip("/") + route, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def get(route):
    with urllib.request.urlopen(URL.rstrip("/") + route) as r:
        return json.load(r)


def patch(route, body=None):
    req = urllib.request.Request(
        URL.rstrip("/") + route, method="PATCH",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def delete(route):
    req = urllib.request.Request(URL.rstrip("/") + route, method="DELETE")
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def status_of(route, body=None, headers=None):
    """POST and report (status, payload) rather than raising on a refusal.

    `post` above sends no X-Client-Id, which is exactly what makes it stand in
    for a terminal; this is how the restriction section checks the 403 it should
    get, and the 200 the browser's own header earns.
    """
    req = urllib.request.Request(
        URL.rstrip("/") + route, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def settings_of(pid):
    """The --settings file a terminal was launched with, parsed."""
    argv = argv_of(pid)
    m = re.search(r"--settings (\S+)", argv)
    path = pathlib.Path(m.group(1)) if m else None
    return path, json.loads(path.read_text()) if path else {}


def argv_of(pid):
    """The command line a pid was really launched with.

    The only way to prove the flags reached the process rather than just the
    code that builds them; -ww so a long argv isn't truncated.
    """
    return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout


def make_fixtures():
    """A clean directory, and no server-side tabs left over from a past run.

    The fixture files are deleted here, but the app keeps its own registry of
    open documents — a stale tab for a path we just removed would make "the tab
    appeared" true before this run created anything.
    """
    for t in get("/api/tabs")["tabs"]:
        if str(FIX) in t["path"] or "/private" + str(FIX) in t["path"]:
            post("/api/close", {"path": t["path"]})
    # Likewise for terminals: an earlier run that died mid-way leaves sessions
    # running, and "no terminal until you ask for one" would fail on them.
    for s in get("/api/terminals")["terminals"]:
        delete(f"/api/terminals/{s['id']}")
    # And sessions: one left behind by a failed run would make "a new session
    # starts empty" true before this run created one. The current session is
    # kept — the last one can't be deleted, by design.
    for s in get("/api/sessions")["sessions"]:
        if not s["current"]:
            delete(f"/api/sessions/{s['id']}")
    # And the skills this suite writes, for the same reason: one surviving from
    # a failed run makes "it was created" true before this run created it.
    for s in get("/api/skills")["skills"]:
        if s["id"].startswith("suite-"):
            delete(f"/api/skills/{s['id']}")
    shutil.rmtree(FIX, ignore_errors=True)
    DEEP.mkdir(parents=True)
    (FIX / "seed.py").write_text("x = 1\n")
    (FIX / "preview.css").write_text("#preview-title { color: rgb(7, 89, 133); }\n")
    (FIX / "preview-data.json").write_text('{"status":"yes"}\n')
    (FIX / "preview.html").write_text(
        "<!doctype html><html><head>"
        "<link rel='stylesheet' href='/preview.css'></head>"
        "<body><h1 id='preview-title'>First render</h1>"
        "<div style='height:1100px'></div>"
        "<p id='preview-scroll-anchor'>Reading position</p>"
        "<div style='height:1100px'></div>"
        "<script>document.body.dataset.scriptRan='yes';"
        "try{parent.document.body.dataset.previewEscaped='yes'}catch(e){};"
        "fetch('/preview-data.json').then(r=>r.json()).then("
        "x=>document.body.dataset.fetched=x.status)"
        "</script></body></html>")
    (FIX / "diagram.svg").write_text(
        "<?xml version=\"1.0\"?><svg xmlns=\"http://www.w3.org/2000/svg\" "
        "width=\"360\" height=\"120\"><rect width=\"360\" height=\"120\" "
        "fill=\"#f8fafc\"/><text id=\"diagram-title\" x=\"20\" y=\"68\" "
        "font-size=\"28\">Diagram title</text></svg>")


def go_to(pg, directory):
    """Point the file panel at `directory` and wait for it to land."""
    target = str(pathlib.Path(directory).resolve())
    pg.evaluate(f"browse('{target}')")
    pg.wait_for_function(f"fileState.path === {json.dumps(target)}", timeout=20000)


def clear_flash(pg):
    """Blank the toast so the next wait can't be satisfied by an old message.

    The element persists between messages, so "#flash.on" on its own is true
    for whatever the previous section flashed.
    """
    pg.evaluate("""() => {
      const el = document.getElementById('flash');
      if (el) { el.classList.remove('on'); el.textContent = ''; }
    }""")


def flash_text(pg, snippet, timeout=20000):
    """Wait for a toast containing `snippet`, and return the whole message."""
    pg.wait_for_function(
        "s => { const el = document.getElementById('flash');"
        "  return el && el.classList.contains('on') && el.textContent.includes(s); }",
        arg=snippet, timeout=timeout)
    return pg.locator("#flash").inner_text()


def answer_name(pg, answer):
    """Answer the in-page name dialog with `answer` (None = cancel).

    Not window.prompt: a browser can suppress native dialogs into a silent
    null, which made every New… quietly do nothing, so naming is in-page now.
    """
    pg.wait_for_selector("#ask-back.on", timeout=15000)
    if answer is None:
        pg.click("#ask-back .tb:not(.primary)")
    else:
        pg.fill("#ask-input", answer)
        pg.click("#ask-ok")


def main():
    make_fixtures()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(URL, wait_until="domcontentloaded")
        # Wait for boot to finish, not just for tabs: its closing browse() would
        # otherwise land after ours and move the directory out from under us.
        pg.wait_for_function("booted", timeout=30000)
        go_to(pg, FIX)

        print("\n-- no terminal until you ask for one")
        check("no sessions", pg.evaluate("terms.length"), 0)
        check("empty state shown", pg.locator("#term-empty").is_visible(), True)
        check("status says so", pg.locator("#ws-status").inner_text(), "No session")
        check("no tab row", pg.locator("#term-tabs .tterm").count(), 0)

        print("\n-- creating a folder")
        pg.evaluate("setTimeout(newFolder, 0)")
        answer_name(pg, "fresh-dir")
        pg.wait_for_function(
            f"fileState.path === '{(FIX / 'fresh-dir').resolve()}'", timeout=20000)
        check("folder created", (FIX / "fresh-dir").is_dir(), True)
        check("stepped into it", pg.evaluate("fileState.path"),
              str((FIX / "fresh-dir").resolve()))

        print("\n-- creating a notebook")
        go_to(pg, FIX)
        pg.evaluate("setTimeout(newNotebook, 0)")
        answer_name(pg, "analysis.ipynb")
        pg.wait_for_function(
            "tabs.some(t => t.name === 'analysis.ipynb')", timeout=25000)
        check("file on disk", (FIX / "analysis.ipynb").is_file(), True)
        check("opened as a notebook tab",
              pg.evaluate("tab(tabs.find(t=>t.name==='analysis.ipynb').path).kind"),
              "notebook")
        nb = json.loads((FIX / "analysis.ipynb").read_text())
        check("real nbformat v4", nb.get("nbformat"), 4)
        check("has a cell to type in", len(nb["cells"]) >= 1, True)

        print("\n-- new file: the extension decides the tab")
        pg.evaluate("setTimeout(newFile, 0)")
        answer_name(pg, "notes.md")
        pg.wait_for_function("tabs.some(t => t.name === 'notes.md')", timeout=20000)
        check("text tab, not notebook",
              pg.evaluate("tab(tabs.find(t=>t.name==='notes.md').path).kind"), "text")
        check("editor is showing", pg.locator("#textpane").is_visible(), True)

        print("\n-- HTML and SVG edit visually inside a sandboxed canvas")
        go_to(pg, FIX)
        pg.evaluate("path => openFile(path)", str(FIX / "preview.html"))
        pg.wait_for_function("activeTab().name === 'preview.html'", timeout=20000)
        preview = pg.frame_locator("#html-preview-frame")
        preview.locator("#preview-title").wait_for(timeout=20000)
        check("only the visual editor is visible",
              (pg.locator("#text-editor").is_visible(),
               pg.locator("#html-preview-frame").is_visible()), (False, True))
        check("renders the HTML",
              preview.locator("#preview-title").inner_text(), "First render")
        check("relative CSS resolves beside the HTML file",
              preview.locator("#preview-title").evaluate(
                  "el => getComputedStyle(el).color"), "rgb(7, 89, 133)")
        check("the iframe uses its own localhost browser origin",
              pg.evaluate("new URL(activeTab().previewUrl).origin !== location.origin"),
              True)
        preview_origin = pg.evaluate("activeTab().previewOrigin")
        # A main-server restart recreates the preview on another random port,
        # commonly with the same generation "1". Simulate the stale client
        # half of that restart and make sure polling reconnects by origin.
        pg.evaluate("activeTab().previewOrigin = 'http://127.0.0.1:9'")
        pg.wait_for_function("origin => activeTab().previewOrigin === origin",
                             arg=preview_origin, timeout=15000)
        preview.locator("#preview-title").wait_for(timeout=20000)
        check("a stale preview origin reconnects automatically",
              pg.evaluate("activeTab().previewOrigin"), preview_origin)
        preview.locator("body[data-fetched='yes']").wait_for(timeout=20000)
        check("root-relative fetch works inside the preview server",
              preview.locator("body").get_attribute("data-fetched"), "yes")
        check("scripts run inside the sandbox",
              preview.locator("body").get_attribute("data-script-ran"), "yes")
        check("the sandbox cannot reach GusNotebook",
              pg.locator("body").get_attribute("data-preview-escaped"), None)

        preview_version = pg.evaluate("activeTab().previewVersion")
        (FIX / "preview.css").write_text(
            "#preview-title { color: rgb(116, 37, 141); }\n")
        pg.wait_for_function("before => activeTab().previewVersion !== before",
                             arg=preview_version, timeout=20000)
        preview.locator("#preview-title").wait_for(timeout=20000)
        check("a served asset change reloads the integrated browser",
              preview.locator("#preview-title").evaluate(
                  "el => getComputedStyle(el).color"), "rgb(116, 37, 141)")

        preview.locator("#preview-title").evaluate("""el => {
          const range = el.ownerDocument.createRange();
          range.selectNodeContents(el);
          const selection = el.ownerDocument.getSelection();
          selection.removeAllRanges(); selection.addRange(range);
          el.ownerDocument.defaultView.focus();
        }""")
        pg.keyboard.insert_text("Edited on the page")
        pg.wait_for_function("activeTab().dirty", timeout=15000)
        check("HTML text edits directly in the page",
              preview.locator("#preview-title").inner_text(), "Edited on the page")
        check("editing marks the HTML tab dirty", pg.evaluate("activeTab().dirty"), True)
        pg.evaluate("saveText(); saveText(); saveText()")
        pg.wait_for_function("!activeTab().dirty", timeout=15000)
        saved_html = (FIX / "preview.html").read_text()
        check("visual HTML edit saves to disk", "Edited on the page" in saved_html, True)
        check("repeated Save clicks do not create a false disk conflict",
              (pg.evaluate("!!activeTab().externalConflict"),
               pg.locator("#ask-back").is_visible()), (False, False))
        check("editor bridge is not written into HTML",
              "data-gusnotebook-runtime" in saved_html, False)

        preview.locator("#preview-title").evaluate("""el => {
          const range = el.ownerDocument.createRange();
          range.selectNodeContents(el);
          const selection = el.ownerDocument.getSelection();
          selection.removeAllRanges(); selection.addRange(range);
          el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        }""")
        pg.wait_for_timeout(350)
        visual_here = get("/api/here")
        check("the visual selection becomes the agent target",
              (visual_here.get("kind"), visual_here.get("selected_text")),
              ("markup", "Edited on the page"))
        check("the agent receives surrounding document context",
              "preview.css" in visual_here.get("document", ""), True)
        preview.locator("#preview-scroll-anchor").evaluate(
            "el => el.scrollIntoView({block: 'start'})")
        pg.wait_for_function(
            "activeTab().markupView && activeTab().markupView.y > 500",
            timeout=10000)
        before_agent_reload = preview.locator("#preview-scroll-anchor").evaluate(
            "el => ({y: scrollY, top: el.getBoundingClientRect().top})")
        replacement = '<em id="agent-revision">Agent revision</em>'
        patch("/api/markup-selection", {
            "selection_id": visual_here["selection_id"], "replacement": replacement})
        preview.locator("#agent-revision").wait_for(timeout=20000)
        check("the agent replacement appears in the live canvas",
              preview.locator("#agent-revision").inner_text(), "Agent revision")
        after_agent_reload = preview.locator("#preview-scroll-anchor").evaluate(
            "el => ({y: scrollY, top: el.getBoundingClientRect().top})")
        check("an agent refresh preserves the reading position",
              (abs(after_agent_reload["y"] - before_agent_reload["y"]) <= 2,
               abs(after_agent_reload["top"] - before_agent_reload["top"]) <= 2),
              (True, True))
        agent_saved = (FIX / "preview.html").read_text()
        check("only the selected bytes were replaced", agent_saved,
              saved_html.replace("Edited on the page", replacement, 1))

        preview.locator("#agent-revision").evaluate("""el => {
          const range = el.ownerDocument.createRange();
          range.selectNodeContents(el);
          const selection = el.ownerDocument.getSelection();
          selection.removeAllRanges(); selection.addRange(range);
          el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        }""")
        pg.wait_for_timeout(350)
        externally_changed = agent_saved.replace(
            "Agent revision", "Agent disk revision", 1).replace(
            "</body>", '<p id="external-note">External reference edit</p></body>')
        (FIX / "preview.html").write_text(externally_changed)
        preview.locator("#external-note").wait_for(timeout=20000)
        preview.locator("#agent-revision", has_text="Agent disk revision").wait_for(
            timeout=20000)
        check("an agent disk save reloads the clean visual canvas",
              (preview.locator("#agent-revision").inner_text(),
               preview.locator("#external-note").inner_text()),
              ("Agent disk revision", "External reference edit"))
        check("the reload reflects exactly what was saved on disk",
              (FIX / "preview.html").read_text(), externally_changed)

        preview.locator("#external-note").evaluate("""el => {
          const range = el.ownerDocument.createRange();
          range.selectNodeContents(el);
          const selection = el.ownerDocument.getSelection();
          selection.removeAllRanges(); selection.addRange(range);
          el.ownerDocument.defaultView.focus();
        }""")
        pg.keyboard.insert_text("Unsaved browser edit")
        pg.wait_for_function("activeTab().dirty", timeout=15000)
        disk_wins = externally_changed.replace(
            "</body>", '<p id="disk-wins">Newer disk edit</p></body>')
        (FIX / "preview.html").write_text(disk_wins)
        pg.wait_for_function("activeTab().externalConflict", timeout=15000)
        check("a disk save does not overwrite an unsaved visual edit",
              pg.locator("#text-status").inner_text(), "changed on disk · reload")
        pg.locator("#text-save").click()
        pg.locator("#ask-back.on").wait_for(timeout=15000)
        check("saving a stale dirty buffer asks before discarding",
              "changed on disk" in pg.locator("#ask-title").inner_text(), True)
        pg.locator("#ask-ok").click()
        preview.locator("#disk-wins").wait_for(timeout=20000)
        check("reload keeps the newer disk content",
              (FIX / "preview.html").read_text(), disk_wins)
        check("reload discards rather than overwrites the stale buffer",
              "Unsaved browser edit" in (FIX / "preview.html").read_text(), False)

        pg.evaluate("path => openFile(path)", str(FIX / "diagram.svg"))
        pg.wait_for_function("activeTab().language === 'svg'", timeout=20000)
        preview.locator("#diagram-title").wait_for(timeout=20000)
        check("SVG opens in the visual editor", pg.evaluate("activeTab().kind"), "text")
        preview.locator("#diagram-title").dblclick()
        svg_input = preview.locator("[data-gusnotebook-runtime='svg-editor']")
        svg_input.wait_for(timeout=10000)
        svg_input.fill("Edited diagram")
        svg_input.press("Enter")
        pg.wait_for_function("activeTab().dirty", timeout=15000)
        check("SVG text edits in place",
              preview.locator("#diagram-title").text_content(), "Edited diagram")
        pg.keyboard.press(("Meta" if sys.platform == "darwin" else "Control") + "+s")
        pg.wait_for_function("!activeTab().dirty", timeout=15000)
        saved_svg = (FIX / "diagram.svg").read_text()
        check("visual SVG edit saves to disk", "Edited diagram" in saved_svg, True)
        check("SVG remains a standalone SVG", "<html" in saved_svg.lower(), False)
        check("SVG prefix is preserved", saved_svg.startswith("<?xml version=\"1.0\"?>"), True)

        svg_origin = pg.evaluate("activeTab().previewOrigin")
        svg_path = pg.evaluate("active")
        pg.evaluate("path => closeTab(path)", svg_path)
        pg.wait_for_function(
            "path => !tabs.some(t => t.path === path)", arg=svg_path, timeout=20000)
        check("closing a visual tab removes its preview server",
              any(p["origin"] == svg_origin for p in get("/api/previews")["previews"]),
              False)

        # A typed .ipynb name goes to the notebook path even via New file.
        pg.evaluate("setTimeout(newFile, 0)")
        answer_name(pg, "second.ipynb")
        pg.wait_for_function("tabs.some(t => t.name === 'second.ipynb')", timeout=25000)
        check("typed .ipynb becomes a notebook",
              pg.evaluate("tab(tabs.find(t=>t.name==='second.ipynb').path).kind"),
              "notebook")

        print("\n-- duplicate names are refused, not overwritten")
        before = (FIX / "seed.py").read_text()
        clear_flash(pg)
        pg.evaluate("setTimeout(newFile, 0)")
        answer_name(pg, "seed.py")
        check("says it exists",
              "already exists" in flash_text(pg, "already exists"), True)
        check("original untouched", (FIX / "seed.py").read_text(), before)

        print("\n-- uploading and downloading from Files")
        go_to(pg, FIX)
        upload_path = FIX / "browser-upload.txt"
        pg.locator("#file-upload").set_input_files({
            "name": upload_path.name,
            "mimeType": "text/plain",
            "buffer": b"uploaded through the Files sidebar\n",
        })
        pg.wait_for_function(
            "name => fileState.entries.some(e => e.name === name)",
            arg=upload_path.name, timeout=20000)
        check("upload writes the document into the browsed directory",
              upload_path.read_text(), "uploaded through the Files sidebar\n")
        check("upload button returns to ready",
              pg.locator("#file-upload-btn").is_enabled(), True)

        row = pg.locator(
            f'#file-list .file-row[title="{upload_path.resolve()}"]')
        row.click(button="right")
        with pg.expect_download(timeout=20000) as download_info:
            pg.locator("#file-ctx .file-ctx-item", has_text="Download").click()
        downloaded = download_info.value
        check("download keeps the file name", downloaded.suggested_filename,
              upload_path.name)
        check("download returns the file bytes",
              pathlib.Path(downloaded.path()).read_bytes(), upload_path.read_bytes())

        before_upload = upload_path.read_bytes()
        clear_flash(pg)
        pg.locator("#file-upload").set_input_files({
            "name": upload_path.name,
            "mimeType": "text/plain",
            "buffer": b"must not overwrite\n",
        })
        check("duplicate upload reports the collision",
              "already exists" in flash_text(pg, "already exists"), True)
        check("duplicate upload leaves the original untouched",
              upload_path.read_bytes(), before_upload)

        print("\n-- cancelling a prompt creates nothing")
        n_before = len(list(FIX.iterdir()))
        pg.evaluate("setTimeout(newFolder, 0)")
        answer_name(pg, None)
        pg.wait_for_timeout(600)
        check("nothing created", len(list(FIX.iterdir())), n_before)

        print("\n-- breadcrumbs truncate to the rightmost folders")
        go_to(pg, DEEP)
        crumbs = pg.locator("#crumbs").inner_text().replace("\n", "")
        print("     crumbs:", crumbs)
        check("collapsed with an ellipsis", "…" in crumbs, True)
        check("keeps the deepest folder", crumbs.endswith("five"), True)
        check("drops the far ancestors", "private" not in crumbs, True)
        box = pg.evaluate(
            "JSON.stringify((({scrollWidth, clientWidth}) => ({scrollWidth, clientWidth}))"
            "(document.getElementById('crumbs')))")
        w = json.loads(box)
        check("fits without scrolling", w["scrollWidth"] <= w["clientWidth"] + 1, True)
        check("full path still available",
              pg.get_attribute("#crumbs", "title"), str(DEEP.resolve()))
        # The "…" is still navigation, not decoration.
        pg.click("#crumbs .crumb")
        pg.wait_for_function(f"fileState.path !== '{DEEP.resolve()}'", timeout=10000)
        check("ellipsis navigates up", pg.evaluate("fileState.path"),
              lambda p: p.endswith("/two"))

        print("\n-- Claude Code terminals, opened where you ask")
        go_to(pg, FIX)
        pg.evaluate("openTerminalHere()")
        pg.wait_for_function("terms.length === 1", timeout=30000)
        check("rooted in the browsed dir", pg.evaluate("terms[0].cwd"),
              str(FIX.resolve()))
        check("tab appeared", pg.locator("#term-tabs .tterm").count(), 1)
        check("empty state hidden", pg.locator("#term-empty").is_visible(), False)
        pg.wait_for_function(
            "terms[0].ws && terms[0].ws.readyState === 1", timeout=30000)
        check("socket connected", pg.locator("#ws-status").inner_text(), "Connected")

        # A second session, rooted somewhere else entirely.
        pg.evaluate(f"openTerminal('{DEEP}')")
        pg.wait_for_function("terms.length === 2", timeout=30000)
        check("two independent sessions", pg.evaluate("terms.length"), 2)
        check("each keeps its own root", pg.evaluate("terms[1].cwd"),
              str(DEEP.resolve()))
        check("roots really differ",
              pg.evaluate("terms[0].cwd !== terms[1].cwd"), True)
        check("second is active", pg.evaluate("activeTerm === terms[1].id"), True)
        check("only the active one is visible",
              pg.locator("#terminal-stack .term-host.on").count(), 1)

        # Claude writes a banner; proves the PTY is really running.
        pg.wait_for_function(
            "terms[1].term.buffer.active.length > 1", timeout=60000)
        check("PTY produced output",
              pg.evaluate("terms[1].term.buffer.active.length > 1"), True)

        first = pg.evaluate("terms[0].id")
        pg.evaluate(f"focusTerm('{first}')")
        check("switching sessions works", pg.evaluate("activeTerm"), first)

        # The header buttons follow Files, even when the active notebook lives
        # elsewhere. That is the directory currently in view and therefore the
        # place where a newly launched agent or shell should start.
        pg.evaluate("switchTab(tabs.find(t => t.name === 'analysis.ipynb').path)")
        pg.wait_for_function("activeTab().name === 'analysis.ipynb'")
        go_to(pg, DEEP)
        pg.get_by_role("button", name="+ Agent", exact=True).click()
        pg.wait_for_function("terms.length === 3", timeout=30000)
        check("new agent follows the Files directory", pg.evaluate("terms[2].cwd"),
              str(DEEP.resolve()))
        pg.get_by_role("button", name="+ Terminal", exact=True).click()
        pg.wait_for_function("terms.length === 4", timeout=30000)
        check("new terminal follows the Files directory", pg.evaluate("terms[3].cwd"),
              str(DEEP.resolve()))
        pg.evaluate("closeTerminal(terms[3].id)")
        pg.wait_for_function("terms.length === 3", timeout=20000)
        pg.evaluate("closeTerminal(terms[2].id)")
        pg.wait_for_function("terms.length === 2", timeout=20000)

        check("agent provider picker offers both CLIs",
              pg.locator("#agent-kind option").all_text_contents(),
              ["Claude", "Codex"])
        if shutil.which("codex"):
            pg.select_option("#agent-kind", "codex")
            pg.get_by_role("button", name="+ Agent", exact=True).click()
            pg.wait_for_function("terms.length === 3", timeout=30000)
            check("picker launches Codex", pg.evaluate("terms[2].kind"), "codex")
            check("Codex follows the Files directory", pg.evaluate("terms[2].cwd"),
                  str(DEEP.resolve()))
            check("Codex runs inline in the embedded terminal",
                  "--no-alt-screen" in argv_of(pg.evaluate("terms[2].pid")), True)
            pg.evaluate("closeTerminal(terms[2].id)")
            pg.wait_for_function("terms.length === 2", timeout=20000)
            pg.select_option("#agent-kind", "claude")

        print("\n-- sessions survive a page reload")
        ids = pg.evaluate("terms.map(t => t.id)")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_function("terms.length === 2", timeout=30000)
        check("reattached, not restarted", pg.evaluate("terms.map(t => t.id)"), ids)
        pg.wait_for_function(
            "terms.every(t => t.ws && t.ws.readyState === 1)", timeout=30000)
        check("scrollback came back",
              pg.evaluate("terms[0].term.buffer.active.length > 1"), True)

        print("\n-- closing a session")
        pg.evaluate(f"closeTerminal('{ids[0]}')")
        pg.wait_for_function("terms.length === 1", timeout=20000)
        check("one left", pg.evaluate("terms.length"), 1)
        check("its host is gone",
              pg.locator("#terminal-stack .term-host").count(), 1)
        pg.evaluate(f"closeTerminal('{ids[1]}')")
        pg.wait_for_function("terms.length === 0", timeout=20000)
        check("back to the empty state", pg.locator("#term-empty").is_visible(), True)

        print("\n-- settings reachable from the title bar, on any kind of tab")
        # The toolbar gear hides with the toolbar on text/image tabs, so the one
        # in the title bar is what makes settings always reachable.
        pg.evaluate("switchTab(tabs.find(t => t.name === 'notes.md').path)")
        pg.wait_for_function("activeTab().kind === 'text'")
        check("toolbar hidden on a text tab", pg.locator("#toolbar").is_visible(), False)
        check("title-bar gear still there",
              pg.locator(".nb-title button[onclick='openSettings()']").is_visible(), True)
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        check("opens from a text tab", pg.locator("#settings-back").is_visible(), True)
        check("and is populated, not blank",
              pg.locator("#set-model option").count() > 1, True)
        pg.evaluate("closeSettings()")

        # A modal that appears before its data would look broken and offer
        # nothing to act on; on failure it must stay shut and say why.
        clear_flash(pg)
        # `api` is a const, so intercept at the fetch layer instead — and only
        # for this one route, so nothing else in the page starts failing.
        pg.evaluate("""() => {
          window.__fetch = window.fetch;
          window.fetch = (u, o) => String(u).includes('/api/settings')
            ? Promise.reject(new Error('gateway down'))
            : window.__fetch(u, o);
        }""")
        pg.evaluate("openSettings().catch(() => {})")
        check("never shown empty on a failed load",
              "Cannot load settings" in flash_text(pg, "Cannot load settings"), True)
        check("modal stayed shut", pg.locator("#settings-back").is_visible(), False)
        pg.evaluate("window.fetch = window.__fetch")

        print("\n-- settings modal")
        pg.evaluate("switchTab(tabs.find(t => t.kind === 'notebook').path)")
        pg.wait_for_function("activeTab().kind === 'notebook'")
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        check("modal open", pg.locator("#settings-back").is_visible(), True)
        check("model list populated",
              pg.locator("#set-model option").count() > 1, True)
        # Against what the API reports, not a hostname literal: the gateway is
        # site-specific and comes from the user's own .env, so a name written into
        # this file would both fail elsewhere and publish where it came from.
        check("shows the gateway it resolved",
              pg.evaluate("settingsData.gateway") in
              pg.locator("#set-gateway").inner_text(), True)
        check("knows a key is present",
              "API key found" in pg.locator("#set-gateway").inner_text(), True)

        original = pg.evaluate("settingsData.settings.inline_llm_model")
        original_instr = pg.evaluate("settingsData.settings.inline_llm_instructions")
        # Pick a model that isn't the current one, so "did the save land?" can't be
        # answered "yes" by the value that was already there when the modal opened.
        target = pg.evaluate(
            "settingsData.models.find(m => m !== settingsData.settings.inline_llm_model)")
        pg.select_option("#set-model", target)
        pg.fill("#set-instructions", "prefer polars")
        pg.click("button.btn.primary")
        # The modal closing is what saveSettings does last, so it's the honest
        # signal that the round trip finished.
        pg.wait_for_selector("#settings-back:not(.on)", state="attached", timeout=20000)
        pg.wait_for_function(
            f"settingsData.settings.inline_llm_model === {json.dumps(target)}",
            timeout=20000)
        check("saved the model choice",
              pg.evaluate("settingsData.settings.inline_llm_model"), target)
        check("saved the instructions",
              pg.evaluate("settingsData.settings.inline_llm_instructions"),
              "prefer polars")
        check("persisted to disk",
              json.loads(settings_file().read_text())["inline_llm_model"],
              target)
        check("modal closed", pg.locator("#settings-back").is_visible(), False)

        print("\n-- gateway credentials, and where the key is kept")
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        check("key field is a password field",
              pg.get_attribute("#set-gw-key", "type"), "password")
        was_source = pg.evaluate("settingsData.key_source")

        # "This session" must keep the key out of settings.json entirely.
        pg.fill("#set-gw-key", "sk-SUITE-SESSION-KEY")
        pg.select_option("#set-gw-store", "session")
        pg.click("button.btn.primary")
        pg.wait_for_function(
            "settingsData.key_source === 'session'", timeout=20000)
        check("key held for the session", pg.evaluate("settingsData.key_source"),
              "session")
        check("never written to settings.json",
              json.loads(settings_file().read_text()).get("gateway_key"),
              lambda v: not v)

        # Reopening shows a mask, never the key — and saving the mask back must
        # not overwrite the stored key with bullets.
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        shown = pg.input_value("#set-gw-key")
        check("shown masked, not in the clear",
              "SUITE" not in shown and bool(shown), True)
        pg.click("button.btn.primary")
        pg.wait_for_selector("#settings-back:not(.on)", state="attached", timeout=20000)
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        check("mask round-trip kept the key",
              pg.evaluate("settingsData.key_source"), "session")

        # Clear it again: the app falls back to the environment / .env.
        pg.fill("#set-gw-key", "")
        pg.click("button.btn.primary")
        pg.wait_for_function(
            "settingsData.key_source !== 'session'", timeout=20000)
        check("falls back once cleared", pg.evaluate("settingsData.key_source"),
              was_source)
        check("still has a working key", pg.evaluate("settingsData.has_key"), True)

        # A model typed by hand wins over the dropdown.
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        pg.fill("#set-model-custom", "some-custom-deployment")
        pg.click("button.btn.primary")
        pg.wait_for_function(
            "settingsData.settings.inline_llm_model === 'some-custom-deployment'",
            timeout=20000)
        check("free-text model accepted",
              pg.evaluate("settingsData.settings.inline_llm_model"),
              "some-custom-deployment")

        # Put the original settings back before the generation test — the suite
        # writes to the real settings.json, so it has to leave it as it found it.
        pg.click(".nb-title button[onclick='openSettings()']")
        pg.wait_for_function("!!settingsData", timeout=20000)
        pg.fill("#set-model-custom", original)
        pg.fill("#set-instructions", original_instr or "")
        pg.click("button.btn.primary")
        pg.wait_for_function(
            f"settingsData.settings.inline_llm_model === {json.dumps(original)}",
            timeout=20000)
        check("restored", pg.evaluate("settingsData.settings.inline_llm_model"), original)
        check("instructions restored",
              pg.evaluate("settingsData.settings.inline_llm_instructions"),
              original_instr or "")

        print("\n-- the + AI cell")
        # In this run's own notebook, not whichever one happened to be first:
        # the project's notebook.ipynb carries cells over between runs, and
        # "find the AI cell" would then find an older one.
        pg.evaluate("switchTab(tabs.find(t => t.name === 'analysis.ipynb').path)")
        pg.wait_for_function("activeTab().name === 'analysis.ipynb'")
        seen = pg.evaluate("cells.map(c => c.id)")
        pg.evaluate("addCell('ai')")
        pg.wait_for_function(
            "old => cells.some(c => c.cell_type === 'ai' && !old.includes(c.id))",
            arg=seen, timeout=20000)
        aid = pg.evaluate(
            "old => cells.find(c => c.cell_type === 'ai' && !old.includes(c.id)).id",
            seen)
        check("gutter marks it AI",
              pg.locator(f".cell[data-id='{aid}'] .gutter-label").inner_text(), "AI")
        check("has a Generate button",
              pg.locator(f"#aigo-{aid}").is_visible(), True)
        check("prompts you in the editor",
              pg.get_attribute(f"#ed-{aid}", "placeholder"),
              lambda s: "plain English" in s)

        # An empty AI cell must not call the model.
        clear_flash(pg)
        pg.evaluate(f"generateCell('{aid}')")
        check("empty prompt refused",
              "Type what you want" in flash_text(pg, "Type what you want"), True)
        check("still an AI cell",
              pg.evaluate(f"getCell('{aid}').cell_type"), "ai")

        if os.environ.get("NO_LLM"):
            print("     (NO_LLM set — skipping the real generation call)")
        else:
            pg.evaluate(f"""(() => {{
              document.getElementById('ed-{aid}').value =
                'assign the string HELLO to a variable called greeting and show it';
            }})()""")
            pg.click(f"#aigo-{aid}")
            pg.wait_for_function(
                f"(getCell('{aid}') || {{}}).cell_type === 'code'", timeout=180000)
            check("became a code cell",
                  pg.evaluate(f"getCell('{aid}').cell_type"), "code")
            src = pg.evaluate(f"getCell('{aid}').source")
            print("     generated:", src.replace("\n", " ⏎ ")[:110])
            check("wrote real Python", "greeting" in src, True)
            check("no markdown fences left", "```" not in src, True)
            check("prompt kept on the cell",
                  pg.evaluate(f"getCell('{aid}').prompt"),
                  lambda s: bool(s) and "HELLO" in s)
            check("prompt strip is shown",
                  pg.locator(f".cell[data-id='{aid}'] .ai-prompt").count(), 1)

            # And the generated code actually runs.
            pg.evaluate(f"runCell('{aid}')")
            pg.wait_for_selector(f"#out-{aid} .outputs", timeout=90000)
            pg.wait_for_function(
                f"!document.querySelector('#out-{aid} .spin')", timeout=90000)
            out = pg.locator(f"#out-{aid}").inner_text()
            print("     output:", out.replace("\n", " ")[:90])
            check("generated code runs", "HELLO" in out, True)

            # The prompt survives a reload, from the .ipynb metadata.
            path = pg.evaluate("active")
            saved = json.loads(pathlib.Path(path).read_text())
            metas = [c.get("metadata", {}).get("inline_prompt")
                     for c in saved["cells"]]
            check("prompt persisted in the .ipynb",
                  any(m and "HELLO" in m for m in metas), True)

        print("\n-- toolbar and add-row offer every cell type")
        labels = [t.strip() for t in pg.locator("#toolbar button.tb").all_inner_texts()]
        print("     toolbar:", labels)
        check("+ Raw and + AI present",
              "+ Raw" in labels and "+ AI" in labels, True)
        check("kernel controls still there",
              "■ Stop" in labels and "↻ Restart" in labels, True)
        row = [t.strip() for t in pg.locator(".add-row button").all_inner_texts()]
        check("add-row matches", row, ["+ Code", "+ Markdown", "+ Raw", "+ AI"])

        print("\n-- the + tab is where you create things")
        check("+ tab is last in the strip", pg.evaluate(
            "document.getElementById('tabs').lastElementChild.id"), "tab-new")
        check("the removed file-panel buttons are gone",
              [t.strip() for t in pg.locator(".files-head button").all_inner_texts()
               if t.strip().startswith("+")], [])
        pg.click("#tab-new")
        pg.wait_for_selector("#new-menu.on", timeout=10000)
        check("six entries, each with an icon",
              [t.strip() for t in pg.locator("#new-menu .nl").all_inner_texts()],
              ["Notebook", "Text file", "Folder", "Claude Code", "Codex",
               "Terminal"])
        check("icons present",
              all(t.strip() for t in pg.locator("#new-menu .ni").all_inner_texts()),
              True)
        pg.keyboard.press("Escape")
        pg.click(".nb-title", position={"x": 5, "y": 5})
        pg.wait_for_timeout(300)
        check("clicking away closes the menu",
              pg.locator("#new-menu").is_visible(), False)

        # Naming is in-page: a browser can suppress window.prompt into a silent
        # null, which made every New… quietly do nothing.
        go_to(pg, FIX)
        pg.evaluate("window.prompt = () => null")
        pg.click("#tab-new")
        pg.wait_for_selector("#new-menu.on", timeout=10000)
        pg.locator("#new-menu .new-item", has_text="Folder").first.click()
        answer_name(pg, "viamenu")
        pg.wait_for_function(
            f"fileState.path === '{(FIX / 'viamenu').resolve()}'", timeout=20000)
        check("creates with native dialogs suppressed",
              (FIX / "viamenu").is_dir(), True)

        print("\n-- the terminal pane is on the app's light theme")
        # The two panes sit side by side all day; a dark terminal read as a
        # different application bolted on.
        def lum(css):
            m = re.findall(r"\d+", css)
            return (0.299 * int(m[0]) + 0.587 * int(m[1]) +
                    0.114 * int(m[2])) if len(m) >= 3 else -1
        check("pane background is light", lum(pg.evaluate(
            "getComputedStyle(document.querySelector('.right')).backgroundColor")),
            lambda v: v > 200)
        check("its bar is light too", lum(pg.evaluate(
            "getComputedStyle(document.querySelector('.right .bar'))"
            ".backgroundColor")), lambda v: v > 200)
        check("text is dark on it", lum(pg.evaluate(
            "getComputedStyle(document.querySelector('.right .bar .title')).color")),
            lambda v: v < 140)

        print("\n-- a terminal is a shell, Claude is Claude")
        go_to(pg, FIX)
        for entry in ("Claude Code", "Terminal"):
            pg.click("#tab-new")
            pg.wait_for_selector("#new-menu.on", timeout=10000)
            pg.locator("#new-menu .new-item", has_text=entry).first.click()
            pg.wait_for_timeout(1500)
        pg.wait_for_function("terms.length >= 2", timeout=20000)
        sessions = get("/api/terminals")["terminals"]
        check("one of each kind", sorted(s["kind"] for s in sessions),
              ["claude", "shell"])
        check("the shell is a real shell",
              any(s["command"].endswith(("zsh", "bash", "sh")) for s in sessions),
              True)
        check("both rooted in the browsed directory",
              all(s["cwd"] == str(FIX.resolve()) for s in sessions), True)
        for s in sessions:
            delete(f"/api/terminals/{s['id']}")

        print("\n-- sessions sit at the foot of the panel, shut by default")
        check("below the file tree", pg.evaluate("""() => {
          const s = document.getElementById('sessions').getBoundingClientRect();
          const f = document.getElementById('file-list').getBoundingClientRect();
          return s.top >= f.bottom - 1;
        }"""), True)
        check("list starts collapsed",
              pg.locator("#session-list").is_visible(), False)
        # Collapsed still has to answer "where am I" — otherwise hiding the list
        # hides the one thing you always need to know.
        check("header names the current session",
              pg.locator("#sessions-cur").inner_text().strip() != "", True)
        pg.click("#sessions .strip-head")
        pg.wait_for_timeout(200)
        check("clicking the header opens it",
              pg.locator("#session-list").is_visible(), True)
        check("exactly one is current",
              pg.locator("#session-list .session-row.current").count(), 1)
        first_session = pg.evaluate("currentSession")
        first_root = pg.evaluate("fileState.path")
        check("each session can open in its own window",
              pg.locator("#session-list .session-row .pop").count() >= 1, True)

        go_to(pg, FIX)
        pg.evaluate("setTimeout(newSession, 0)")
        answer_name(pg, "Second")
        pg.wait_for_function(
            f"currentSession && currentSession !== {json.dumps(first_session)}",
            timeout=30000)
        check("a new session starts empty", pg.evaluate("tabs.length"), 0)
        check("rooted where the panel was", pg.evaluate("fileState.path"),
              str(FIX.resolve()))

        # Switching is a view change, not a teardown: the whole point is that a
        # kernel left running elsewhere is still running when you come back.
        sess = {s["id"]: s for s in get("/api/sessions")["sessions"]}
        check("the other session kept its tabs",
              len(sess[first_session]["tabs"]) > 0, True)
        check("and they're not on screen here",
              pg.evaluate("tabs.length"), 0)
        second = pg.evaluate("currentSession")

        # A session is page-scoped, not one mutable server-global. Two browser
        # windows can stay on separate workspaces and switching either one must
        # not pull the other page along with it.
        peer = b.new_page(viewport={"width": 1200, "height": 800})
        peer.on("pageerror", lambda e: errors.append("peer: " + str(e)))
        peer.goto(URL + ("&" if "?" in URL else "?") +
                  "session=" + urllib.parse.quote(first_session),
                  wait_until="domcontentloaded")
        peer.wait_for_function("booted", timeout=30000)
        check("a second window restores the requested session",
              peer.evaluate("currentSession"), first_session)
        peer.evaluate(f"switchSession({json.dumps(second)})")
        peer.wait_for_function(
            f"currentSession === {json.dumps(second)}", timeout=30000)
        peer.evaluate(f"switchSession({json.dumps(first_session)})")
        peer.wait_for_function(
            f"currentSession === {json.dumps(first_session)}", timeout=30000)
        check("the original window remains independent",
              pg.evaluate("currentSession"), second)
        peer.close()

        pg.evaluate(f"switchSession({json.dumps(first_session)})")
        pg.wait_for_function(
            f"currentSession === {json.dumps(first_session)}", timeout=30000)
        check("switching back restores its tabs",
              pg.evaluate("tabs.length") == len(sess[first_session]["tabs"]), True)
        check("switching back restores its Files location",
              pg.evaluate("fileState.path"), first_root)

        # Deleting is the one destructive path — it must not take the files.
        (FIX / "kept.py").write_text("still here\n")
        pg.evaluate(f"switchSession({json.dumps(second)})")
        pg.wait_for_function(
            f"currentSession === {json.dumps(second)}", timeout=30000)
        pg.evaluate(f"openFile('{(FIX / 'kept.py').resolve()}')")
        pg.wait_for_function("tabs.length === 1", timeout=20000)
        pg.evaluate(f"switchSession({json.dumps(first_session)})")
        pg.wait_for_function(
            f"currentSession === {json.dumps(first_session)}", timeout=30000)
        pg.evaluate(f"setTimeout(() => deleteSession({json.dumps(second)}), 0)")
        pg.wait_for_selector("#ask-back.on", timeout=15000)
        # askConfirm, not window.confirm — a suppressed confirm() returns false,
        # which would make this silently refuse.
        check("confirmed in-page, with no name to type",
              pg.locator("#ask-input").is_visible(), False)
        pg.click("#ask-ok")
        pg.wait_for_function("sessionList.length === 1", timeout=30000)
        check("its tab was released", any(
            t["path"].endswith("kept.py") for t in get("/api/tabs")["all_tabs"]),
            False)
        check("the file stayed on disk", (FIX / "kept.py").is_file(), True)

        pg.evaluate(f"setTimeout(() => deleteSession({json.dumps(first_session)}), 0)")
        pg.wait_for_selector("#ask-back.on", timeout=15000)
        pg.click("#ask-ok")
        check("the last session is refused, with a reason",
              "last session" in flash_text(pg, "last session"), True)

        print("\n-- skills: one markdown file, two consumers")
        check("above Sessions, below the tree", pg.evaluate("""() => {
          const k = document.getElementById('skills').getBoundingClientRect();
          const s = document.getElementById('sessions').getBoundingClientRect();
          const f = document.getElementById('file-list').getBoundingClientRect();
          return k.top >= f.bottom - 1 && k.bottom <= s.top + 1;
        }"""), True)
        check("shut by default", pg.locator("#skill-list").is_visible(), False)
        pg.click("#skills .strip-head")
        pg.wait_for_timeout(200)
        check("opens on click", pg.locator("#skill-list").is_visible(), True)
        # The description is what tells you whether this is the snippet you want,
        # so a row without one is a row you can't choose from.
        check("every row carries a description", pg.evaluate(
            "[...document.querySelectorAll('.skill-row .ds')]"
            ".every(e => e.textContent.trim())"), True)

        pg.evaluate("setTimeout(() => newSkill(), 0)")
        pg.wait_for_selector("#skill-back.on", timeout=15000)
        check("nothing to delete on a new one",
              pg.locator("#skill-del").is_visible(), False)
        pg.fill("#skill-name", "Suite Skill")
        # The name *is* the /command, so it's slugged — shown as you type rather
        # than silently rewritten when you save.
        check("previews the /command it becomes",
              pg.locator("#skill-id-preview").inner_text(), "/suite-skill")
        pg.fill("#skill-desc", "Written by the test suite.")
        pg.fill("#skill-body",
                "Use when testing.\n\n```python\nsuite_marker = 7\n```\n\n"
                "Not like this:\n\n```python\nwrong = True\n```")
        pg.click("#skill-back .btn.primary")
        pg.wait_for_function("skillList.some(s => s.id === 'suite-skill')",
                             timeout=20000)
        made = next(s for s in get("/api/skills")["skills"]
                    if s["id"] == "suite-skill")
        # The *first* block only: a later one is usually the counter-example, and
        # concatenating them would produce a cell that contradicts itself.
        check("only the first code block is the snippet",
              made["code"], "suite_marker = 7")
        check("front matter is what Claude reads", pathlib.Path(made["path"])
              .read_text().startswith("---\nname: suite-skill\ndescription: "),
              True)

        # Clicking inserts a cell. No model call — if you know which snippet you
        # want, waiting on a generation to reproduce it is a step backwards.
        post("/api/files/new", {"directory": str(FIX), "name": "skill.ipynb",
                                "kind": "file"})
        pg.evaluate(f"openFile('{(FIX / 'skill.ipynb').resolve()}')")
        pg.wait_for_function(
            f"tab({json.dumps(str((FIX / 'skill.ipynb').resolve()))})",
            timeout=20000)
        before = pg.evaluate("cells.length")
        pg.evaluate("insertSkill('suite-skill')")
        pg.wait_for_function(f"cells.length === {before + 1}", timeout=20000)
        cid = pg.evaluate("cells[cells.length-1].id")
        src = pg.eval_on_selector(f"#ed-{cid}", "e => e.value")
        check("inserted as a code cell",
              pg.evaluate("cells[cells.length-1].cell_type"), "code")
        check("the code, without the fences", src, "suite_marker = 7")
        # Saved, not just typed into the DOM: addCell made an empty cell
        # server-side, so a reload would otherwise lose the snippet.
        check("saved to the notebook", any(
            c["id"] == cid and c["source"] == "suite_marker = 7"
            for c in get("/api/notebook?notebook=" + urllib.parse.quote(
                str((FIX / "skill.ipynb").resolve())))["cells"]), True)

        # Renaming moves the directory, because the directory name is the
        # /command — leaving it behind would give Claude the old name.
        pg.evaluate("setTimeout(() => openSkill('suite-skill'), 0)")
        pg.wait_for_selector("#skill-back.on", timeout=15000)
        pg.fill("#skill-name", "suite renamed")
        pg.click("#skill-back .btn.primary")
        pg.wait_for_function("skillList.some(s => s.id === 'suite-renamed')",
                             timeout=20000)
        ids = [s["id"] for s in get("/api/skills")["skills"]]
        check("renamed, with no stale copy",
              ("suite-renamed" in ids, "suite-skill" in ids), (True, False))
        pg.evaluate("setTimeout(() => openSkill('suite-renamed'), 0)")
        pg.wait_for_selector("#skill-back.on", timeout=15000)
        pg.click("#skill-del")
        # The confirm is asked *from* the editor, so it has to paint above it —
        # at a shared z-index it was behind the modal and unclickable.
        pg.wait_for_selector("#ask-back.on", timeout=15000)
        check("its confirm is clickable above the editor",
              pg.evaluate("""() => {
                const r = document.getElementById('ask-ok').getBoundingClientRect();
                const el = document.elementFromPoint(r.left + r.width / 2,
                                                     r.top + r.height / 2);
                return el && el.id === 'ask-ok';
              }"""), True)
        pg.click("#ask-ok")
        pg.wait_for_function("!skillList.some(s => s.id === 'suite-renamed')",
                             timeout=20000)
        check("deleted", any(s["id"] == "suite-renamed"
                             for s in get("/api/skills")["skills"]), False)

        print("\n-- standing instructions reach both agents, but not a shell")
        pg.evaluate("setTimeout(() => openSettings(), 0)")
        pg.wait_for_selector("#settings-back.on", timeout=15000)
        # A separate field from the inline LLM's: different model, different job.
        check("its own field, not the + AI one", pg.evaluate(
            "document.getElementById('set-claude') !== "
            "document.getElementById('set-instructions')"), True)
        pg.fill("#set-claude", "SUITE_GLOBAL_RULE")
        pg.click("#settings-back .btn.primary")
        pg.wait_for_selector("#settings-back.on", state="hidden", timeout=15000)
        sid_now = pg.evaluate("currentSession")
        pg.evaluate(f"setTimeout(() => openSessionInstr({json.dumps(sid_now)}), 0)")
        pg.wait_for_selector("#sinstr-back.on", timeout=15000)
        pg.fill("#sinstr-text", "SUITE_SESSION_RULE")
        pg.click("#sinstr-back .btn.primary")
        pg.wait_for_selector("#sinstr-back.on", state="hidden", timeout=15000)
        pg.wait_for_function("sessionList.some(s => s.instructions)", timeout=20000)
        # Instructions you can't see are the ones that surprise you when an agent
        # follows them, so a set note stays visible unhovered.
        check("the row shows it's set",
              pg.locator(".session-row.current .note.set").count(), 1)

        claude = post("/api/terminals", {"cwd": str(FIX), "kind": "claude"})
        argv = argv_of(claude["pid"])
        check("instructions injected into Claude",
              "--append-system-prompt-file" in argv, True)
        check("skills passed as a plugin", "--plugin-dir" in argv, True)
        m = re.search(r"--append-system-prompt-file (\S+)", argv)
        body = pathlib.Path(m.group(1)).read_text() if m else ""
        check("both layers present, app-wide first",
              ("SUITE_GLOBAL_RULE" in body, "SUITE_SESSION_RULE" in body,
               body.find("SUITE_GLOBAL_RULE") < body.find("SUITE_SESSION_RULE")),
              (True, True, True))
        delete(f"/api/terminals/{claude['id']}")
        # One temp file per Claude session would otherwise pile up all run.
        check("the temp prompt file goes with the session",
              pathlib.Path(m.group(1)).exists() if m else None, False)

        if shutil.which("codex"):
            codex = post("/api/terminals", {"cwd": str(FIX), "kind": "codex"})
            codex_argv = argv_of(codex["pid"])
            check("Codex receives both instruction layers",
                  ("developer_instructions=" in codex_argv,
                   "SUITE_GLOBAL_RULE" in codex_argv,
                   "SUITE_SESSION_RULE" in codex_argv),
                  (True, True, True))
            check("Codex receives the current-cell hook",
                  "hooks.UserPromptSubmit=" in codex_argv, True)
            delete(f"/api/terminals/{codex['id']}")

        # A shell gets none of it — and deliberately no gateway token either.
        shell = post("/api/terminals", {"cwd": str(FIX), "kind": "shell"})
        sh_argv = argv_of(shell["pid"])
        check("a plain shell is left plain",
              ("--append-system-prompt" in sh_argv, "--plugin-dir" in sh_argv),
              (False, False))
        delete(f"/api/terminals/{shell['id']}")

        # Put the app back as it was: these are user settings, not fixtures.
        post("/api/settings", {"claude_instructions": ""})
        post(f"/api/sessions/{sid_now}", {"instructions": ""})

        print("\n-- restrictions: rules Claude Code enforces, not asks about")
        # Cleared first, and checked cleared: residue from a past run would
        # pre-satisfy every assertion below and hide a feature that stopped
        # working. Off is also the default, and the thing most easily broken.
        post("/api/settings", {"claude_restrictions": {}})
        post(f"/api/sessions/{sid_now}", {"restrictions": {}})
        plain = post("/api/terminals", {"cwd": str(FIX), "kind": "claude"})
        p0, spec0 = settings_of(plain["pid"])
        # An unrestricted session's argv is byte-for-byte what it was before the
        # feature existed — no permissions block at all, not an empty one.
        check("nothing restricted means nothing in the settings file",
              sorted(spec0), ["hooks"])
        delete(f"/api/terminals/{plain['id']}")

        pg.evaluate("setTimeout(() => openSettings(), 0)")
        pg.wait_for_selector("#settings-back.on", timeout=15000)
        pg.wait_for_function(
            "document.querySelectorAll('#set-restrict input').length === 4",
            timeout=15000)
        check("all four presets unticked to start", pg.evaluate(
            "[...document.querySelectorAll('#set-restrict input')]"
            ".map(b => b.checked)"), [False] * 4)
        pg.check("#set-restrict input[value=no_execute]")
        pg.fill("#set-restrict-extra", "# a comment\nBash(rm *)")
        pg.click("#settings-back .btn.primary")
        pg.wait_for_selector("#settings-back.on", state="hidden", timeout=15000)
        # Unticked keys are left out rather than written false: {} is what
        # "nothing restricted" means everywhere else in this feature.
        check("saved as the ticked keys only",
              get("/api/settings")["settings"]["claude_restrictions"],
              {"no_execute": True, "deny_extra": "# a comment\nBash(rm *)"})

        pg.evaluate(f"setTimeout(() => openSessionInstr({json.dumps(sid_now)}), 0)")
        pg.wait_for_selector("#sinstr-back.on", timeout=15000)
        pg.wait_for_function(
            "document.querySelectorAll('#sinstr-restrict input').length === 4",
            timeout=15000)
        pg.check("#sinstr-restrict input[value=no_bash_read]")
        pg.click("#sinstr-back .btn.primary")
        pg.wait_for_function("sessionList.some(s => s.restrictions.no_bash_read)",
                             timeout=20000)
        # Same rule as the instructions: a restriction you can't see is exactly
        # the one that surprises you when Claude obeys it.
        check("the row's note stays visible for a restriction alone",
              pg.locator(".session-row.current .note.set").count(), 1)

        locked = post("/api/terminals", {"cwd": str(FIX), "kind": "claude"})
        lpath, lspec = settings_of(locked["pid"])
        deny = lspec.get("permissions", {}).get("deny", [])
        check("the app-wide preset reached the process",
              "Bash(python *)" in deny, True)
        check("so did the session's, unioned rather than replacing it",
              "Bash(cat *)" in deny, True)
        # The "identify structure" half of the request, deliberately untouched:
        # a tree you can list is not a file you can read.
        check("ls, find, wc and stat are left alone",
              [any(r.startswith(f"Bash({c}") for r in deny)
               for c in ("ls", "find", "wc", "stat")], [False] * 4)
        check("a hand-written rule passes through", "Bash(rm *)" in deny, True)
        check("a # comment line does not",
              any(r.startswith("#") for r in deny), False)
        # The rules go in the app's own --settings file, beside the hook, and
        # never in ~/.claude/settings.json, which belongs to the user.
        check("the hook still registered alongside them",
              "UserPromptSubmit" in lspec.get("hooks", {}), True)
        m2 = re.search(r"--append-system-prompt-file (\S+)", argv_of(locked["pid"]))
        note = pathlib.Path(m2.group(1)).read_text() if m2 else ""
        # Rules do the enforcing; the note stops Claude spending a turn
        # discovering the wall by walking into it.
        check("and Claude is told what's blocked, and that it's enforced",
              ("enforced by Claude Code itself" in note, "Running code" in note),
              (True, True))

        # Execution is blocked in the app, not by a rule: `gusnb add` runs a cell
        # on a live kernel, and deny rules don't reach a subprocess that opens
        # files itself. Only the app can refuse, since the app owns the kernel.
        rnb = post("/api/open", {"path": str(FIX / "restrict.ipynb")})["path"]
        rq = "?notebook=" + urllib.parse.quote(rnb)
        rcell = post("/api/cells" + rq, {"cell_type": "code", "source": "1+1"})
        code, refused = status_of(f"/api/cells/{rcell['id']}/run" + rq,
                                 {"source": "2+2"})
        check("a header-less run is refused", code, 403)
        check("with something the user can act on",
              "press ▶ to run it" in (refused.get("error") or ""), True)
        # The source is still written: the cell Claude wrote must survive the
        # refusal, or "write it and let the user run it" is impossible.
        check("but the cell was written anyway",
              [c["source"] for c in get("/api/notebook" + rq)["cells"]
               if c["id"] == rcell["id"]], ["2+2"])
        check("run-all too", status_of("/api/run-all" + rq)[0], 403)
        # A guardrail against Claude's own tools, not a boundary: the header can
        # be forged. Deny rules have the same property.
        check("the browser's own header still runs",
              status_of(f"/api/cells/{rcell['id']}/run" + rq, {},
                        {"X-Client-Id": "suite"})[0], 200)

        delete(f"/api/terminals/{locked['id']}")
        check("both temp files go with the session",
              [lpath.exists(), pathlib.Path(m2.group(1)).exists() if m2 else None],
              [False, False])

        post("/api/settings", {"claude_restrictions": {}})
        post(f"/api/sessions/{sid_now}", {"restrictions": {}})
        check("cleared, and the run API answers again",
              status_of(f"/api/cells/{rcell['id']}/run" + rq)[0], 200)
        delete(f"/api/cells/{rcell['id']}" + rq)

        print("\n-- the cell the user is on, and per-cell undo")
        # The point of entry for "work on this cell": Claude reads it without
        # being told an id, replaces it, and the user can put it back.
        # Opened through the API first, for its normalized path: /tmp is a
        # symlink to /private/tmp here, and the browser keys tabs by what the
        # server returns, not by what was asked for.
        hpath = post("/api/open", {"path": str(FIX / "here.ipynb")})["path"]
        pg.evaluate(f"openFile({hpath!r})")
        pg.wait_for_function(f"active === {hpath!r}", timeout=15000)
        typed = "# typed by the user\nkeep_me = 1\n"
        made = post("/api/cells?notebook=" + urllib.parse.quote(hpath),
                    {"cell_type": "code", "source": typed})
        cid = made["id"]
        pg.evaluate("load()")
        pg.wait_for_function(f"cells.some(c => c.id === {cid!r})", timeout=15000)

        # Focus follows the click, and reaches the server — which is the whole
        # reason `here` can answer at all.
        pg.click(f"#ed-{cid}")
        pg.wait_for_timeout(400)
        here = get("/api/here")
        check("the focused cell is what the browser clicked", here.get("cell_id"), cid)
        check("it names the notebook the caret is in", here.get("notebook"), hpath)
        # No ?notebook= — the hook can't pass one, so this must answer alone
        # rather than falling back to the session's first notebook.
        check("answers unqualified, not via the session's first tab",
              here["cell"]["source"], typed)

        # A replace is undoable; the user's own typing is not, or every keystroke
        # pause would bury the one entry that matters.
        q = "?notebook=" + urllib.parse.quote(hpath)
        agent_src = "# replaced by an agent\nkeep_me = 99\n"
        patch(f"/api/cells/{cid}{q}", {"source": agent_src, "undoable": True})
        cells_now = get("/api/notebook" + q)["cells"]
        check("the replace is recorded",
              next(c["undo_depth"] for c in cells_now if c["id"] == cid), 1)
        patch(f"/api/cells/{cid}{q}", {"source": agent_src + "# typing\n"})
        cells_now = get("/api/notebook" + q)["cells"]
        check("ordinary typing isn't",
              next(c["undo_depth"] for c in cells_now if c["id"] == cid), 1)

        # A second cell's history must be independent — JupyterLab's behaviour,
        # and the reason this lives in cell metadata rather than one stack.
        other = post("/api/cells" + q, {"cell_type": "code", "source": "other = 1"})
        patch(f"/api/cells/{other['id']}{q}",
              {"source": "other = 2", "undoable": True})

        pg.evaluate("load()")
        pg.wait_for_function(
            f"(cells.find(c => c.id === {cid!r}) || {{}}).undo_depth === 1", timeout=15000)
        check("the cell offers an undo",
              pg.locator(f'.cell[data-id="{cid}"] .undo-bar').count(), 1)
        # The affordance is per cell, so it must be absent where nothing was
        # replaced — counted against a cell that really exists, since a selector
        # matching nothing would pass this whatever the UI did.
        plain = post("/api/cells" + q, {"cell_type": "code", "source": "plain = 1"})
        pg.evaluate("load()")
        pg.wait_for_function(
            f"cells.some(c => c.id === {plain['id']!r})", timeout=15000)
        check("a cell with no history offers none", pg.evaluate(
            f"""document.querySelectorAll(
                  '.cell[data-id="{plain['id']}"] .undo-bar').length"""), 0)
        pg.click(f'.cell[data-id="{cid}"] .undo-go')
        pg.wait_for_function(
            f"cells.find(c => c.id === {cid!r}).source.startsWith('# typed')",
            timeout=20000)
        check("undo restores what the user had typed",
              pg.evaluate(f"cells.find(c => c.id === {cid!r}).source"), typed)
        # Outputs belonged to the source that produced them.
        check("its outputs went with it",
              pg.evaluate(f"cells.find(c => c.id === {cid!r}).outputs.length"), 0)
        check("undoing here left the other cell alone", pg.evaluate(
            f"cells.find(c => c.id === {other['id']!r}).undo_depth"), 1)
        # Exhausted, not silently repeating: a second undo would otherwise wipe
        # source with nothing to restore.
        try:
            post(f"/api/cells/{cid}/undo{q}")
            refused = False
        except urllib.error.HTTPError as e:
            refused = e.code == 400
        check("a second undo is refused, not a no-op", refused, True)

        print("\n-- the cell reaches Claude on every prompt, by hook")
        claude2 = post("/api/terminals", {"cwd": str(FIX), "kind": "claude"})
        argv2 = argv_of(claude2["pid"])
        check("a settings file is passed", "--settings" in argv2, True)
        m2 = re.search(r"--settings (\S+)", argv2)
        spec = json.loads(pathlib.Path(m2.group(1)).read_text()) if m2 else {}
        hooks = spec.get("hooks", {}).get("UserPromptSubmit", [])
        script = hooks[0]["hooks"][0]["command"] if hooks else ""
        check("it registers a UserPromptSubmit hook", bool(script), True)
        # Run the hook as Claude would, and check the focused cell comes out:
        # this is what puts the cell in front of the model with no /command.
        out = subprocess.run(["sh", script], input='{"prompt":"x"}',
                             capture_output=True, text=True, timeout=60).stdout
        check("the hook emits the focused cell", cid in out and "keep_me" in out, True)
        delete(f"/api/terminals/{claude2['id']}")
        check("hook and settings go with the session",
              (pathlib.Path(m2.group(1)).exists(), pathlib.Path(script).exists()),
              (False, False))
        # A shell gets no hook either — it has no model to give context to.
        shell2 = post("/api/terminals", {"cwd": str(FIX), "kind": "shell"})
        check("no hook in a plain shell",
              "--settings" in argv_of(shell2["pid"]), False)
        delete(f"/api/terminals/{shell2['id']}")

        print("\n-- the cell editor: CodeMirror, with a textarea behind it")
        # Colouring is asserted on span *count*, not by eye: two copies of
        # @codemirror/state make CM's instanceof checks fail, and the result is
        # an editor that renders and edits normally with zero highlighted
        # tokens — plain-looking text with nothing in the console. The `*`
        # prefix in the import map is what prevents it, and this is the check
        # that would catch its removal.
        epath = post("/api/open", {"path": str(FIX / "editor.ipynb")})["path"]
        pg.evaluate(f"openFile({epath!r})")
        pg.wait_for_function(f"active === {epath!r}", timeout=15000)
        eq = "?notebook=" + urllib.parse.quote(epath)
        py = ('import os\n'
              '# a comment\n'
              'class Thing:\n'
              '    def run(self, n=42):\n'
              '        return f"hello {n}" if n else None\n')
        ecid = post("/api/cells" + eq, {"cell_type": "code", "source": py})["id"]
        pg.evaluate("load()")
        pg.wait_for_function(f"cmViews.has({ecid!r})", timeout=20000)
        esel = f'.cell[data-id="{ecid}"] .cm-line span'
        check("Python is tokenised",
              pg.eval_on_selector_all(esel, "els => els.length"), lambda n: n > 0)
        check("in more than one colour", len(pg.eval_on_selector_all(
            esel, "els => [...new Set(els.map(e => getComputedStyle(e).color))]")),
              lambda n: n > 1)

        # Eight call sites and this suite address the editor as `#ed-<id>.value`.
        # CM has no `.value`, so the host is given one — without it every one of
        # them silently reads undefined.
        check("#ed-<id> still answers to .value",
              pg.eval_on_selector(f"#ed-{ecid}", "e => e.value"), py)
        pg.evaluate(f"document.getElementById('ed-{ecid}').value = 'shimmed = 1\\n'")
        check("and writing it reaches the document",
              pg.evaluate(f"cmViews.get({ecid!r}).state.doc.toString()"), "shimmed = 1\n")

        # Typing history is per editor, which is JupyterLab's model and the same
        # principle nb_undo follows: undoing here says nothing about the cell
        # below. Distinct from the ↶ Undo replace strip above, which is the
        # server-side stack for what an agent or a snippet wrote.
        e2 = post("/api/cells" + eq, {"cell_type": "code", "source": "b = 2\n"})["id"]
        pg.evaluate("load()")
        pg.wait_for_function(f"cmViews.has({e2!r})", timeout=20000)
        pg.click(f"#ed-{ecid} .cm-content")
        pg.keyboard.press("End")
        pg.keyboard.type("x = 1")
        pg.wait_for_function(
            f"CM.undoDepth(cmViews.get({ecid!r}).state) > 0", timeout=15000)
        check("↶ offered once there's something to undo",
              pg.eval_on_selector(f"#hist-{ecid} .hist-btn", "e => e.disabled"), False)
        before = pg.evaluate(f"document.getElementById('ed-{e2}').value")
        pg.evaluate(f"cellUndo({ecid!r})")
        pg.wait_for_timeout(300)
        check("undo took back the typing",
              pg.evaluate(f"document.getElementById('ed-{ecid}').value"),
              lambda s: "x = 1" not in s)
        check("and left the other cell alone",
              pg.evaluate(f"document.getElementById('ed-{e2}').value"), before)
        pg.evaluate(f"cellRedo({ecid!r})")
        pg.wait_for_timeout(300)
        check("redo put it back",
              pg.evaluate(f"document.getElementById('ed-{ecid}').value"),
              lambda s: "x = 1" in s)

        # render() replaces the notebook's innerHTML wholesale. A view rebuilt on
        # each render loses its history every time anything repaints — adding a
        # cell, a run finishing, a kernel event — so the view is moved instead.
        pg.wait_for_function(f"!unsaved.has({ecid!r})", timeout=15000)
        depth = pg.evaluate(f"CM.undoDepth(cmViews.get({ecid!r}).state)")
        pg.evaluate("render()")
        pg.wait_for_timeout(300)
        check("history survives a re-render",
              pg.evaluate(f"CM.undoDepth(cmViews.get({ecid!r}).state)"), depth)

        # A re-render inside the save debounce must not push the stale
        # cells[].source back over what is still being typed.
        pg.click(f"#ed-{ecid} .cm-content")
        pg.keyboard.press("Meta+a" if sys.platform == "darwin" else "Control+a")
        pg.keyboard.type("half_typed = ")
        check("the cell is known to be unsaved",
              pg.evaluate(f"unsaved.has({ecid!r})"), True)
        pg.evaluate("render()")
        pg.wait_for_timeout(250)
        check("a render mid-typing keeps the keystrokes",
              pg.evaluate(f"document.getElementById('ed-{ecid}').value"), "half_typed = ")

        # An agent's `gusnb here - --run` lands under the editor. The view is
        # rebuilt rather than patched: dispatching the replacement leaves CM
        # rebasing its history through a whole-document change, and ⌘Z then
        # yields a half-and-half document that is neither what the user typed nor
        # what the agent wrote — which the debounced save would then persist.
        pg.wait_for_function(f"!unsaved.has({ecid!r})", timeout=15000)
        patch(f"/api/cells/{ecid}{eq}",
              {"source": "agent_wrote = 2\n", "undoable": True})
        pg.wait_for_function(
            f"(getCell({ecid!r}) || {{}}).source === 'agent_wrote = 2\\n'",
            timeout=20000)
        check("the agent's write reached the editor",
              pg.evaluate(f"document.getElementById('ed-{ecid}').value"),
              "agent_wrote = 2\n")
        check("no ⌘Z back over it",
              pg.evaluate(f"CM.undoDepth(cmViews.get({ecid!r}).state)"), 0)
        check("the server-side undo is what's offered instead",
              pg.locator(f'.cell[data-id="{ecid}"] .undo-bar').count(), 1)

        # Every binding onEditorKey had, as CM commands.
        pg.evaluate(f"document.getElementById('ed-{e2}').value = 'q = 7\\nr = 8\\n'")
        pg.click(f"#ed-{e2} .cm-content")
        mod = "Meta" if sys.platform == "darwin" else "Control"
        pg.keyboard.press(f"{mod}+a")
        pg.keyboard.press(f"{mod}+/")
        pg.wait_for_timeout(400)
        check("⌘/ comments the selected lines",
              pg.evaluate(f"document.getElementById('ed-{e2}').value"),
              lambda s: s.startswith("# q = 7") and "# r = 8" in s)
        pg.keyboard.press(f"{mod}+/")
        pg.wait_for_timeout(400)
        check("and uncomments them again",
              pg.evaluate(f"document.getElementById('ed-{e2}').value"),
              lambda s: s.startswith("q = 7"))
        # Tab indents by the indentUnit rather than inserting a literal \t,
        # which in Python source is exactly what nobody wants.
        pg.evaluate(f"document.getElementById('ed-{e2}').value = 'z = 0'")
        pg.click(f"#ed-{e2} .cm-content")
        pg.keyboard.press("Home")
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(300)
        check("Tab inserts spaces, not a tab character",
              pg.evaluate(f"document.getElementById('ed-{e2}').value"), "    z = 0")
        # ⇧⏎ must run once. The document-level handler treats CM's content div as
        # claimed; without that the cell runs twice, from both keymaps.
        pg.evaluate(
            f"document.getElementById('ed-{e2}').value = " + json.dumps(
                "import time; print('working', flush=True); time.sleep(1); print(6*7)"))
        pg.click(f"#ed-{e2} .cm-content")
        pg.keyboard.press("Shift+Enter")
        pg.wait_for_selector(f"#out-{e2} .spin", timeout=10000)
        check("running uses animated progress dots", pg.eval_on_selector(
            f"#out-{e2} .spin",
            "e => getComputedStyle(e, '::after').animationName"),
              "progress-dots")
        pg.wait_for_function(
            f"document.getElementById('out-{e2}').innerText.includes('working')",
            timeout=10000)
        check("streamed output keeps the running indicator",
              pg.locator(f"#out-{e2} .spin").count(), 1)
        check("the active cell animates its execution number",
              pg.locator(f'.cell[data-id="{e2}"].is-running .run-symbol').count(), 1)
        check("the execution number cycles between + and *", pg.eval_on_selector(
            f'.cell[data-id="{e2}"] .run-symbol',
            "e => getComputedStyle(e, '::before').animationName"),
              "cell-run-symbol")
        pg.wait_for_selector(f"#out-{e2} .outputs", timeout=60000)
        pg.wait_for_function(f"!document.querySelector('#out-{e2} .spin')", timeout=60000)
        check("completion removes the gutter animation",
              pg.locator(f'.cell[data-id="{e2}"] .run-symbol').count(), 0)
        check("⇧⏎ runs the cell",
              pg.locator(f"#out-{e2}").inner_text(), lambda s: "42" in s)
        check("exactly once", next(
            (c["execution_count"] for c in get("/api/notebook" + eq)["cells"]
             if c["id"] == e2), None), 1)

        print("\n-- output scroll hands off to the notebook")
        scroll_box = pg.evaluate(f"""() => {{
          const out = document.getElementById('out-{e2}');
          out.dataset.testOriginal = out.innerHTML;
          out.innerHTML = '<div class="outputs"><div class="output stream">' +
            Array.from({{length: 120}}, (_, i) => 'line ' + i).join('\\n') +
            '</div></div>';
          const spacer = document.createElement('div');
          spacer.id = 'scroll-handoff-spacer'; spacer.style.height = '900px';
          out.closest('.cell').after(spacer);
          const stream = out.querySelector('.output.stream');
          stream.scrollIntoView({{block: 'center'}});
          stream.scrollTop = stream.scrollHeight;
          const pane = document.getElementById('notebook-pane');
          const rect = stream.getBoundingClientRect();
          return {{x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
                   before: pane.scrollTop}};
        }}""")
        pg.mouse.move(scroll_box["x"], scroll_box["y"])
        pg.mouse.wheel(0, 500)
        pg.wait_for_timeout(400)
        after_scroll = pg.evaluate(
            "document.getElementById('notebook-pane').scrollTop")
        check("wheel continues into the notebook at the output's bottom",
              after_scroll > scroll_box["before"], True)
        pg.evaluate(f"""() => {{
          document.getElementById('scroll-handoff-spacer').remove();
          const out = document.getElementById('out-{e2}');
          out.innerHTML = out.dataset.testOriginal; delete out.dataset.testOriginal;
        }}""")

        # Rendered markdown has no editor at all, and an editing one gets no
        # Python grammar.
        emd = post("/api/cells" + eq,
                   {"cell_type": "markdown", "source": "## heading"})["id"]
        pg.evaluate("load()")
        pg.wait_for_function(
            f"!!document.querySelector('.cell[data-id=\"{emd}\"] .md-rendered')",
            timeout=20000)
        check("rendered markdown mounts no editor",
              pg.evaluate(f"cmViews.has({emd!r})"), False)
        pg.evaluate(f"editMarkdown({emd!r})")
        pg.wait_for_function(f"cmViews.has({emd!r})", timeout=20000)
        check("markdown gets no Python colouring", pg.eval_on_selector_all(
            f'.cell[data-id="{emd}"] .cm-line span', "els => els.length"), 0)
        # A view left behind would point at a detached node, and its history
        # would surface on whatever cell next reused the id.
        pg.evaluate(f"deleteCell({emd!r})")
        pg.wait_for_function(f"!cmViews.has({emd!r})", timeout=20000)
        check("a deleted cell's view is forgotten",
              pg.evaluate(f"cmViews.has({emd!r})"), False)

        print("\n-- with the CDN unreachable, the textarea is still there")
        # Load-bearing rather than decorative: CM is ESM from a CDN, and if the
        # import fails there is no editor at all — a notebook that cannot be
        # typed in is the alternative to this fallback.
        off = b.new_page()
        off.route("**esm.sh**", lambda r: r.abort())
        off.goto(URL)
        off.wait_for_function("window.CM_READY === true", timeout=60000)
        check("CM is reported absent, not half-loaded",
              off.evaluate("window.CM"), None)
        ocid = post("/api/cells" + eq, {"cell_type": "code", "source": ""})["id"]
        # After the tab bar has been restored: switching before boot finishes is
        # undone by the restore that follows it.
        off.wait_for_function(f"tab({epath!r})", timeout=20000)
        off.evaluate(f"switchTab({epath!r})")
        off.wait_for_function(f"active === {epath!r}", timeout=20000)
        off.wait_for_selector(f"#ed-{ocid}", timeout=20000)
        check("the editor is a plain textarea",
              off.eval_on_selector(f"#ed-{ocid}", "e => e.tagName"), "TEXTAREA")
        check("no history buttons are offered",
              off.eval_on_selector_all(f"#hist-{ocid}", "els => els.length"), 0)
        off.click(f"#ed-{ocid}")
        off.keyboard.type("print('fallback works')")
        off.keyboard.press("Shift+Enter")
        off.wait_for_selector(f"#out-{ocid} .outputs", timeout=60000)
        off.wait_for_function(
            f"!document.querySelector('#out-{ocid} .spin')", timeout=60000)
        check("typing and ⇧⏎ still work",
              off.locator(f"#out-{ocid}").inner_text(),
              lambda s: "fallback works" in s)
        off.close()

        print("\n-- the cell you're on stays marked when focus leaves")
        # The whole point of a class rather than :focus — focus goes to the Claude
        # pane exactly when you most need to see which cell Claude will act on.
        pg.click(f"#ed-{ecid} .cm-content")
        check("the clicked cell is current", pg.eval_on_selector_all(
            ".cell.is-current", "els => els.map(e => e.dataset.id)"), [ecid])
        pg.evaluate("document.activeElement.blur(); document.body.focus()")
        check("and stays current with focus gone", pg.eval_on_selector_all(
            ".cell.is-current", "els => els.map(e => e.dataset.id)"), [ecid])
        pg.evaluate("render()")
        check("and through a repaint", pg.eval_on_selector_all(
            ".cell.is-current", "els => els.map(e => e.dataset.id)"), [ecid])

        print("\n-- the gutter's + and ✕")
        before = len(get("/api/notebook" + eq)["cells"])
        pg.click(f'.cell[data-id="{ecid}"] .gutter-acts '
                 '.act-btn[title="Add a code cell below"]')
        pg.wait_for_function(f"cells.length === {before + 1}", timeout=20000)
        added = pg.evaluate(
            f"cells[cells.findIndex(c => c.id === {ecid!r}) + 1].id")
        # Always code: a markdown cell is a note about code you're about to
        # write, so code is what you want next.
        check("+ adds a code cell directly below",
              pg.evaluate(f"getCell({added!r}).cell_type"), "code")
        check("and parks you on it", pg.evaluate("selected"), added)
        pg.click(f'.cell[data-id="{added}"] .act-btn.danger')
        pg.wait_for_function(f"!getCell({added!r})", timeout=20000)
        check("✕ deletes it", len(get("/api/notebook" + eq)["cells"]), before)
        # Deleting the cell you're on leaves you on a neighbour, as JupyterLab
        # does — not on nothing.
        check("the highlight lands on a neighbour",
              pg.evaluate("!!selected && !!getCell(selected)"), True)

        print("\n-- long code cells arrive folded")
        long_src = "\n".join(f"v{i} = {i}" for i in range(25))
        fcid = post("/api/cells" + eq,
                    {"cell_type": "code", "source": long_src})["id"]
        pg.evaluate("load()")
        pg.wait_for_selector(f'.cell[data-id="{fcid}"] .fold.folded', timeout=20000)
        fold = f'.cell[data-id="{fcid}"] .fold'
        clipped = pg.eval_on_selector(fold, "e => e.getBoundingClientRect().height")
        full = pg.eval_on_selector(f"#ed-{fcid} .cm-content",
                                   "e => e.getBoundingClientRect().height")
        check("it is really clipped, not just styled",
              clipped < full - 40, lambda v: v)
        check("the veil says how much is hidden", pg.eval_on_selector(
            f'.cell[data-id="{fcid}"] .fold-veil span', "e => e.textContent"),
              lambda s: "15 more lines" in s)
        check("the veil is the transparency", pg.eval_on_selector(
            f'.cell[data-id="{fcid}"] .fold-veil',
            "e => getComputedStyle(e).backgroundImage"),
              lambda s: "gradient" in s)
        pg.click(f'.cell[data-id="{fcid}"] .fold-veil')
        check("clicking it opens the cell", pg.eval_on_selector(
            fold, "e => e.classList.contains('folded')"), False)
        check("and the veil goes with the fold", pg.eval_on_selector_all(
            f'.cell[data-id="{fcid}"] .fold-veil', "els => els.length"), 0)
        pg.click(f"#foldb-{fcid}")
        check("the gutter ⌃ folds it again", pg.eval_on_selector(
            fold, "e => e.classList.contains('folded')"), True)
        pg.evaluate("render()")
        check("the fold survives a repaint", pg.eval_on_selector(
            fold, "e => e.classList.contains('folded')"), True)
        check("a short cell isn't wrapped at all", pg.eval_on_selector_all(
            f'.cell[data-id="{ecid}"] .fold', "els => els.length"), 0)
        # A cell grows past the threshold *while you type in it*, and a repaint —
        # a run finishing, a kernel event — would otherwise fold the code away
        # with the caret in the hidden part. Typed rather than PATCHed, because
        # the caret is the whole point.
        gcid = post("/api/cells" + eq, {"cell_type": "code"})["id"]
        pg.evaluate("load()")
        pg.wait_for_selector(f"#ed-{gcid} .cm-content", timeout=20000)
        pg.click(f"#ed-{gcid} .cm-content")
        for i in range(14):
            pg.keyboard.type(f"y{i} = {i}")
            pg.keyboard.press("Enter")
        pg.wait_for_function(f"!unsaved.has({gcid!r})", timeout=20000)
        pg.evaluate("render()")
        check("the cell you're typing in is never folded",
              pg.eval_on_selector_all(
                  f'.cell[data-id="{gcid}"] .fold.folded', "els => els.length"), 0)
        check("and none of the typing was lost",
              pg.evaluate(f"document.getElementById('ed-{gcid}').value"),
              lambda s: "y13 = 13" in s)
        pg.evaluate("document.activeElement.blur(); render()")
        check("it folds once you leave it", pg.eval_on_selector_all(
            f'.cell[data-id="{gcid}"] .fold.folded', "els => els.length"), 1)

        print("\n-- output collapses, and stays collapsed through a re-run")
        ocid2 = post("/api/cells" + eq,
                     {"cell_type": "code", "source": "print('shown')"})["id"]
        pg.evaluate("load()")
        pg.wait_for_selector(f"#ed-{ocid2}", timeout=20000)
        pg.evaluate(f"runCell({ocid2!r})")
        pg.wait_for_selector(f"#out-{ocid2} .outputs", timeout=60000)
        pg.wait_for_function(
            f"!document.querySelector('#out-{ocid2} .spin')", timeout=60000)
        # The ▾ has to appear without a re-render: the SSE handlers write into
        # #out-<id> and never rebuild the gutter.
        pg.wait_for_selector(f"#outb-{ocid2}", timeout=20000)
        check("running a cell gives it a ▾", pg.eval_on_selector(
            f"#outb-{ocid2}", "e => e.textContent.trim()"), "▾")
        pg.click(f"#outb-{ocid2}")
        check("clicking hides the output", pg.eval_on_selector(
            f"#out-{ocid2}", "e => getComputedStyle(e).display"), "none")
        # Hidden, not forgotten: a collapsed cell must not read as one that
        # never ran.
        check("a note stands in for it", pg.eval_on_selector(
            f'.cell[data-id="{ocid2}"] .out-note', "e => e.textContent"),
              lambda s: "hidden" in s)
        pg.evaluate("render()")
        check("hidden survives a repaint", pg.eval_on_selector(
            f"#out-{ocid2}", "e => getComputedStyle(e).display"), "none")
        pg.evaluate(f"runCell({ocid2!r})")
        pg.wait_for_timeout(2500)
        check("a re-run leaves it collapsed", pg.eval_on_selector(
            f"#out-{ocid2}", "e => getComputedStyle(e).display"), "none")
        check("with one note, not two per run", pg.eval_on_selector_all(
            f'.cell[data-id="{ocid2}"] .out-note', "els => els.length"), 1)
        pg.click(f'.cell[data-id="{ocid2}"] .out-note')
        check("the note opens it again", pg.eval_on_selector(
            f"#out-{ocid2}", "e => getComputedStyle(e).display"),
              lambda s: s != "none")
        check("the output is still there", pg.locator(f"#out-{ocid2}").inner_text(),
              lambda s: "shown" in s)
        check("a cell that never ran has no ▾", pg.eval_on_selector_all(
            f"#outb-{fcid}", "els => els.length"), 0)

        print("\n-- the gutter's type menu")
        pg.click(f'.cell[data-id="{ocid2}"] .gutter-acts .act-btn:first-child')
        check("it opens", pg.eval_on_selector(
            "#type-menu", "e => e.classList.contains('on')"), True)
        pg.click("#type-menu .new-item:has-text('Markdown')")
        pg.wait_for_function(
            f"getCell({ocid2!r}) && getCell({ocid2!r}).cell_type === 'markdown'",
            timeout=20000)
        check("markdown reaches the file", next(
            (c["cell_type"] for c in get("/api/notebook" + eq)["cells"]
             if c["id"] == ocid2), None), "markdown")
        check("and the menu closes", pg.eval_on_selector(
            "#type-menu", "e => e.classList.contains('on')"), False)
        pg.click(f'.cell[data-id="{ocid2}"] .gutter-acts .act-btn:first-child')
        pg.click("#type-menu .new-item:has-text('Code')")
        pg.wait_for_function(
            f"getCell({ocid2!r}).cell_type === 'code'", timeout=20000)
        check("and back to code",
              pg.evaluate(f"getCell({ocid2!r}).cell_type"), "code")
        pg.click(f'.cell[data-id="{ecid}"] .gutter-acts .act-btn:first-child')
        pg.click("body", position={"x": 4, "y": 4})
        check("clicking away dismisses it", pg.eval_on_selector(
            "#type-menu", "e => e.classList.contains('on')"), False)

        print("\n-- a cell Claude rewrote says what was asked")
        # Recorded by the same UserPromptSubmit hook that injects the focused
        # cell — it already reads the payload, and `prompt` is in it.
        pcid = post("/api/cells" + eq,
                    {"cell_type": "code", "source": "old = 1"})["id"]
        post("/api/prompt", {"prompt": "use a dataframe here instead"})
        # `undoable` is what marks a write as not-the-user's-typing, which is the
        # only signal available: the CLI has an id but not the prompt behind it.
        patch(f"/api/cells/{pcid}" + eq, {"source": "df = None", "undoable": True})
        pcell = next(c for c in get("/api/notebook" + eq)["cells"]
                     if c["id"] == pcid)
        check("the prompt lands on the cell", pcell["claude_prompt"],
              "use a dataframe here instead")
        check("separately from the inline LLM's", pcell["prompt"], None)
        pg.evaluate("load()")
        pg.wait_for_selector(f'.cell[data-id="{pcid}"] .ai-prompt.claude',
                             timeout=20000)
        check("the strip shows it", pg.eval_on_selector(
            f'.cell[data-id="{pcid}"] .ai-prompt.claude .pt',
            "e => e.textContent.trim()"), "use a dataframe here instead")
        # No ↻: an inline prompt is a self-contained request the app can send
        # again; this was one turn of a terminal conversation.
        check("with no ↻ on it", pg.eval_on_selector_all(
            f'.cell[data-id="{pcid}"] .ai-prompt.claude .re', "els => els.length"), 0)
        # A caption describing source that's been walked back is a wrong caption.
        pg.click(f'.cell[data-id="{pcid}"] .undo-go')
        pg.wait_for_function(
            f"getCell({pcid!r}) && getCell({pcid!r}).source === 'old = 1'",
            timeout=20000)
        check("undo takes the caption with the source", pg.eval_on_selector_all(
            f'.cell[data-id="{pcid}"] .ai-prompt.claude', "els => els.length"), 0)
        # Typing is not an agent's write, and must not be captioned as one.
        post("/api/prompt", {"prompt": "a prompt that is still live"})
        tcid = post("/api/cells" + eq, {"cell_type": "code"})["id"]
        patch(f"/api/cells/{tcid}" + eq, {"source": "typed = 1"})
        check("a plain PATCH is never captioned", next(
            (c["claude_prompt"] for c in get("/api/notebook" + eq)["cells"]
             if c["id"] == tcid), "missing"), None)
        # The last prompt is remembered for PROMPT_TTL, so leaving one set would
        # caption whatever the rest of this suite writes with `undoable`.
        post("/api/prompt", {"prompt": ""})

        print("\n-- dotfiles are listed by default")
        # .env and .gitignore are working files here, so hiding them by default
        # hid things the user came to edit.
        pg.evaluate(f"browse('{APP_DIR}')")
        pg.wait_for_function(f"fileState.path === '{APP_DIR}'", timeout=10000)
        names = pg.eval_on_selector_all(
            "#file-list .file-row", "els => els.map(e => e.title.split('/').pop())")
        check("dotfiles shown", any(n.startswith(".") for n in names), True)
        check(".* button lit to match the state",
              pg.evaluate("document.getElementById('hidden-btn')"
                          ".classList.contains('on')"), True)
        check(".git still skipped", ".git" in names, False)

        print("\n-- hiding the file panel doesn't shift the other panes")
        # Regression: `display: none` on the files pane stopped it occupying its
        # grid column, sliding every later pane one column left — the editor
        # collapsed to 0px and the splitter grew to swallow every click.
        pg.evaluate("toggleFiles()")
        pg.wait_for_timeout(300)
        check("editor keeps its width", pg.evaluate(
            "Math.round(document.querySelector('.left').getBoundingClientRect().width)"),
            lambda w: w > 400)
        check("splitter stays a thin strip", pg.evaluate(
            "Math.round(document.getElementById('splitter')"
            ".getBoundingClientRect().width)"), lambda w: w <= 10)
        pg.evaluate("toggleFiles()")
        pg.wait_for_timeout(300)

        print("\n-- the stylesheet and the eight scripts are served and applied")
        # The page is a template plus static assets now, not one 4600-line file.
        # Everything else in this suite asserts on behaviour, which mostly still
        # works with a 404'd stylesheet — so this asserts on computed style and on
        # the globals, the two things the split can actually break.
        check("one stylesheet, from the app's own static/", pg.evaluate(
            "[...document.styleSheets]"
            ".filter(s => (s.href || '').includes('/static/app.css')).length"), 1)
        check("the palette resolved", pg.evaluate(
            "getComputedStyle(document.documentElement)"
            ".getPropertyValue('--panel').trim() !== ''"), True)
        check("an early rule applies", pg.evaluate(
            "getComputedStyle(document.getElementById('app')).boxSizing"),
            "border-box")
        # .flash is the last rule in the file and its element is created lazily,
        # so this says the tail of the stylesheet arrived, not just the head.
        pg.evaluate("flash('suite: stylesheet check')")
        pg.wait_for_selector("#flash", timeout=10000)
        check("and so does the last one in the file", pg.evaluate(
            "getComputedStyle(document.getElementById('flash')).position"), "fixed")
        # One name from each file, in load order: a script that 404s or throws
        # takes its own globals with it and nothing else, which is otherwise
        # invisible until the one feature it owns is used.
        # Named directly rather than as window[...]: `api` is a top-level const,
        # which lands in the global *lexical* scope and never on window, unlike
        # the seven function declarations. Both are reachable from a later file
        # and from an inline handler, which is what actually matters here.
        check("all eight scripts ran", pg.evaluate(
            "[typeof api, typeof browse, typeof render, typeof mountEditor,"
            " typeof runCell, typeof load, typeof loadSessions, typeof fitTerm]"),
            ["function"] * 8)
        # Why these are classic scripts and not ES modules: a module's top-level
        # bindings are module-scoped, so every inline handler in the markup would
        # resolve to nothing.
        check("inline onclick= still finds them", pg.evaluate(
            """() => {
                 const el = document.querySelector('[onclick*="openSettings"]');
                 return !!el && typeof window.openSettings === 'function';
               }"""), True)

        print("\n-- state lives outside the package, work inside the launch dir")
        # The whole reason this is installable: an installed package directory is
        # shared between projects, often read-only, and replaced on upgrade, so
        # nothing writable may live in it. These checks are cheap and the failure
        # they catch is expensive — a `pip install --upgrade` that eats the user's
        # settings, sessions and skills.
        st = get("/api/settings")
        state_dir = pathlib.Path(st["state_dir"])
        settings_path = pathlib.Path(st["settings_path"])
        skills_dir = pathlib.Path(get("/api/skills")["dir"])
        # Asked of the interpreter, not computed from this file: the point is
        # where the *installed* package sits, which is somewhere else entirely
        # once the app is installed rather than run from a checkout.
        pkg_dir = pathlib.Path(subprocess.run(
            [sys.executable, "-c", "import gusnotebook, pathlib;"
             "print(pathlib.Path(gusnotebook.__file__).resolve().parent)"],
            capture_output=True, text=True, timeout=60).stdout.strip() or ".")
        check("the app reports where its state is", state_dir.is_dir(), True)
        check("settings.json is in the state dir",
              settings_path.parent == state_dir, True)
        check("settings.json exists where it says", settings_path.is_file(), True)
        check("skills are in the state dir too",
              str(skills_dir).startswith(str(state_dir)), True)
        check("no state inside the package",
              [str(p).startswith(str(pkg_dir)) for p in (settings_path, skills_dir)],
              [False, False])
        check("the package ships its template",
              (pkg_dir / "templates" / "index.html").is_file(), True)
        # And its assets. A wheel that builds but 404s its own stylesheet is the
        # failure mode; asked of the installed package, like everything here.
        check("and the stylesheet and scripts beside it",
              [(pkg_dir / "static" / "app.css").is_file(),
               sorted(p.name for p in (pkg_dir / "static" / "js").glob("*.js"))],
              [True, ["actions.js", "browser.js", "cells.js", "core.js",
                      "editor.js", "events.js", "panels.js", "terminals.js"]])
        # Work is separate again: the file browser and new tabs start where the
        # user launched the app, not where the code happens to be installed.
        work = pathlib.Path(get("/api/files")["cwd"])
        check("work dir isn't the state dir", work == state_dir, False)
        check("work dir isn't the package dir", work == pkg_dir, False)

        print("\n-- the hook resolves gusnb rather than assuming a path")
        # It used to be BASE_DIR / "nb.py", which doesn't exist once installed.
        # An absolute path that's really there is the only version that survives
        # a PTY child with a different PATH.
        claude3 = post("/api/terminals", {"cwd": str(FIX), "kind": "claude"})
        m3 = re.search(r"--settings (\S+)", argv_of(claude3["pid"]))
        spec3 = json.loads(pathlib.Path(m3.group(1)).read_text()) if m3 else {}
        hook3 = spec3["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        text3 = pathlib.Path(hook3).read_text()
        m4 = re.search(r"cell=\$\((\S+) here", text3)
        nb_cmd = m4.group(1) if m4 else ""
        check("the hook calls gusnb by absolute path",
              nb_cmd.startswith("/") and nb_cmd.endswith("gusnb"), True)
        check("and that file exists", pathlib.Path(nb_cmd).is_file() if nb_cmd
              else False, True)
        check("it passes the app's own URL", f"NB_URL={URL.rstrip('/')}" in text3
              or "NB_URL=http://127.0.0.1:8888" in text3, True)
        delete(f"/api/terminals/{claude3['id']}")

        print("\n-- nothing broke")
        cols = pg.evaluate(
            "getComputedStyle(document.getElementById('app')).gridTemplateColumns")
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
