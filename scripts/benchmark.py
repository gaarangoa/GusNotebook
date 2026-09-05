"""Measure document save costs and browser rendering on disposable fixtures."""

import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

import nbformat
from gusnotebook.notebook import Notebook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from ui_server import isolated_server, launch_browser


def document_benchmark():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "large.ipynb"
        notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(
            f"value = {index}", outputs=[nbformat.v4.new_output("stream", name="stdout", text="x" * 10000)])
            for index in range(300)])
        nbformat.write(notebook, path)
        document = Notebook(path)
        cell = document.to_json()["cells"][0]
        timings = {}
        for mode in ("changed_save_ms", "unchanged_save_ms"):
            samples = []
            for index in range(10):
                source = f"value = {index}" if mode == "changed_save_ms" else document.cell_json(cell["id"])["source"]
                start = time.perf_counter()
                document.update_cell(cell["id"], source=source)
                samples.append((time.perf_counter() - start) * 1000)
            timings[mode] = round(statistics.median(samples), 2)
        return {"cells": 300, "bytes": path.stat().st_size, **timings}


def browser_benchmark():
    from playwright.sync_api import sync_playwright
    with isolated_server() as (url, token, _root, _env), sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.goto(url + "#token=" + token)
        page.wait_for_function("typeof cells !== 'undefined' && cells.length && window.CM")
        result = page.evaluate("""() => {
          cells = Array.from({length: 200}, (_, index) => ({id: 'bench' + index,
            cell_type: 'code', source: 'value = ' + index + '\\nprint(value)', outputs: []}));
          activeTab().cells = cells;
          const fullRender = () => {
            const scroll = document.getElementById('notebook-pane').scrollTop;
            document.getElementById('notebook').innerHTML = cells.map(cellHtml).join('');
            mountEditors(); paintSelection(); resetFoldedPreviewScrolls();
            document.querySelectorAll('.editor').forEach(autosize);
            pinStreams(document.getElementById('notebook')); applyHeadingCollapse();
            document.getElementById('notebook-pane').scrollTop = scroll;
          };
          const measure = fn => {
            fn(); fn();
            const samples = [];
            for (let i = 0; i < 7; i++) {
              const start = performance.now(); fn(); samples.push(performance.now() - start);
            }
            return samples.sort((a,b) => a-b)[3];
          };
          return {cells: cells.length, full_render_ms: measure(fullRender),
                  incremental_render_ms: measure(render)};
        }""")
        browser.close()
        return {key: round(value, 2) for key, value in result.items()}


if __name__ == "__main__":
    print(json.dumps({"documents": document_benchmark(), "browser": browser_benchmark()}, indent=2))
