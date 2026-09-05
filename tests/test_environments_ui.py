"""Real uv creation, editable repository installation and notebook use in a disposable app."""

import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    root = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]).resolve()
    work = root / "work"
    repository = work / "local repo's source"
    repository.mkdir()
    source = repository / "env_fixture.py"
    source.write_text("MESSAGE = 'first'\n")
    (repository / "pyproject.toml").write_text('''[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"
[project]
name = "gusnb-local-env-fixture"
version = "1.2.3"
[tool.setuptools]
py-modules = ["env_fixture"]
''')
    target = work / "analysis env's"

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("typeof cells !== 'undefined' && cells.length && activeTab().python")
        notebook = page.evaluate("active")

        page.click("#tab-new")
        page.locator("#new-menu").get_by_text("Environment", exact=True).click()
        expect(page.locator("#environment-submit")).to_be_enabled()
        page.click("#environment-packages-tab")
        page.wait_for_function("environmentState.info && environmentState.info.packages.length")
        page.fill("#environment-filter", "ipykernel")
        expect(page.locator("#environment-package-rows")).to_contain_text(importlib.metadata.version("ipykernel"))
        assert page.locator("#environment-package-rows tr").count() == 1
        print("PASS: + menu opens environments and shows installed versions with filtering", flush=True)

        page.click("#environment-create-tab")
        page.fill("#environment-name", target.name)
        page.fill("#environment-python", sys.executable)
        # Exercise the folder picker as well as path entry and apostrophes in folder names.
        page.locator("#environment-create").get_by_text("Browse…", exact=True).click()
        page.fill("#dirpick-manual", str(work))
        page.click("#dirpick-ok")
        expect(page.locator("#environment-location")).to_have_value(str(work))
        page.locator("#environment-create").get_by_text("Add folder…", exact=True).click()
        page.locator("#dirpick-list .dpick-row").filter(has_text=repository.name).click()
        expect(page.locator("#dirpick-sel")).to_have_text("Selected: " + str(repository))
        page.click("#dirpick-ok")
        expect(page.locator("#environment-repositories")).to_have_value(str(repository))
        page.fill("#environment-requirements", "packaging==" + importlib.metadata.version("packaging"))
        before = page.evaluate("cells.map(c => c.execution_count)")
        page.locator("#environment-requirements").press("Shift+Enter")
        assert page.evaluate("cells.map(c => c.execution_count)") == before
        assert not page.evaluate("cells.some(c => c._running)")
        page.screenshot(path=str(Path(tempfile.gettempdir()) / "gusnb-create-environment.png"))
        page.click("#environment-submit")
        page.wait_for_function("environmentState.job && environmentState.job.id")
        job_id = page.evaluate("environmentState.job.id")
        # Closing the modal leaves creation running, and reopening resumes its progress.
        page.get_by_role("button", name="Close environments", exact=True).click()
        page.click("#tab-new")
        page.locator("#new-menu").get_by_text("Environment", exact=True).click()
        page.wait_for_function("environmentState.job && ['ready', 'failed', 'cancelled'].includes(environmentState.job.status)", timeout=180000)
        job = page.evaluate("environmentState.job")
        assert job["id"] == job_id and job["status"] == "ready", job
        expect(page.locator("#environment-packages")).to_be_visible()
        page.fill("#environment-filter", "gusnb-local-env-fixture")
        row = page.locator("#environment-package-rows")
        expect(row).to_contain_text("1.2.3")
        expect(row).to_contain_text(str(repository))
        expect(row).to_contain_text("(editable)")
        assert (target / "pyvenv.cfg").is_file()
        registry = json.loads((root / "state/environments.json").read_text())
        assert any(entry["prefix"] == str(target) for entry in registry)
        assert any(p["name"].lower() == "ipykernel" for p in job["environment"]["packages"])
        print("PASS: uv creates a named environment, installs pinned packages and an editable repository, and records it", flush=True)

        page.screenshot(path=str(Path(tempfile.gettempdir()) / "gusnb-installed-packages.png"))
        page.click("#environment-use")
        expect(page.locator("#environment-back")).not_to_have_class("modal-back on", timeout=60000)
        interpreter = job["environment"]["python"]
        assert page.evaluate("activeTab().python") == interpreter

        def run(code):
            return page.evaluate("""async code => {
              const id = cells[0].id;
              document.getElementById('ed-' + id).value = code;
              await runCell(id);
              return document.getElementById('out-' + id).innerText;
            }""", code)

        output = run("import sys, env_fixture\nprint(sys.executable)\nprint(env_fixture.MESSAGE)")
        assert interpreter in output and "first" in output, output
        # Change size too: CPython may otherwise reuse same-second bytecode.
        source.write_text("MESSAGE = 'second edit'\n")
        assert "second edit" in run("import importlib\nimportlib.reload(env_fixture)\nprint(env_fixture.MESSAGE)")
        assert json.loads(Path(notebook).read_text())["metadata"]["kernelspec"]["notebook_python"] == interpreter
        print("PASS: notebook uses the new interpreter and sees local source edits without reinstalling", flush=True)

        page.click("#venv-btn")
        page.locator("#venv-menu .venv-item").filter(has_text=target.name).get_by_role("button", name="Packages", exact=True).click(timeout=30000)
        page.wait_for_function("environmentState.info && environmentState.info.python === activeTab().python")
        expect(page.locator("#environment-package-info")).to_contain_text(interpreter)
        page.click("#environment-create-tab")
        page.click("#environment-submit")
        expect(page.locator("#environment-error")).to_contain_text("already exists")
        assert (target / "pyvenv.cfg").is_file()

        # A real uv resolver failure must remove the newly created directory.
        page.fill("#environment-name", "failed-install")
        page.fill("#environment-requirements", "packaging==0.0.0")
        page.fill("#environment-repositories", "")
        page.click("#environment-submit")
        page.wait_for_function("environmentState.job && environmentState.job.name === 'failed-install' && environmentState.job.status === 'failed'", timeout=90000)
        expect(page.locator("#environment-error")).to_be_visible()
        assert not (work / "failed-install").exists()
        assert source.read_text() == "MESSAGE = 'second edit'\n"
        print("PASS: picker inspection, existing folder protection, and failed uv installation cleanup", flush=True)
        assert not errors, errors
        browser.close()
        print("PASS: no browser JavaScript errors", flush=True)


if __name__ == "__main__":
    main()
