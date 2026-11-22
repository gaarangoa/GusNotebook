"""Real D3 notebook figures and exported frames render without external assets."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import nbformat
from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated

from gusnotebook.plotting import D3_EXAMPLE
from gusnotebook.skills import code_of


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    notebook = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']).resolve() / 'work' / 'd3.ipynb'
    code = code_of(D3_EXAMPLE)
    nbformat.write(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(code)]), notebook)
    html = []
    with patch('IPython.display.display', side_effect=html.append):
        exec(code, {})
    source = html[0].data
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={'width': 1440, 'height': 960})
        external, errors = [], []

        def route(request):
            host = urlsplit(request.request.url).hostname or ''
            if request.request.url.startswith(url) or host == '127.0.0.1' or host.endswith('.localhost'):
                request.continue_()
            else:
                external.append(request.request.url)
                request.abort()

        context.route('**/*', route)
        authenticate_browser(context, url)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('path => openFile(path)', str(notebook))
        page.wait_for_function('cells.length === 1 && cells[0].source.includes("d3.select")')
        page.evaluate('async () => { await runCell(cells[0].id); }')
        frame = page.frame_locator('iframe[data-output-frame]').first
        expect(frame.locator('svg .bar')).to_have_count(3)
        expect(frame.locator('svg')).to_have_attribute('viewBox', '0 0 640 320')
        expect(frame.locator('svg > title')).to_have_text('Values by category')
        expect(frame.locator('script[src]')).to_have_count(0)
        assert page.locator('iframe[data-output-frame]').first.get_attribute('sandbox') == 'allow-scripts'
        assert not external, external
        assert not errors, errors
        print('PASS: executed notebook cell renders bundled D3 without external requests', flush=True)

        page.reload(wait_until='domcontentloaded')
        expect(frame.locator('svg .bar')).to_have_count(3)
        for theme in ('light', 'dark'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            expect(frame.locator('svg .bar')).to_have_count(3)
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-d3.png'))

        # The terminal agent embeds the exact executed output in arbitrary HTML,
        # with the generating Python AND D3 still visible in the notebook.
        cell_id = page.evaluate('cells[0].id')
        fragment = notebook.parent / 'figure.html'
        cli = [str(Path(sys.executable).parent / 'gusnb'), '-n', str(notebook)]
        exported_cli = subprocess.run(cli + ['export-output', cell_id, '--file', str(fragment)],
                                      capture_output=True, text=True, timeout=30)
        assert exported_cli.returncode == 0, exported_cli.stderr
        report = notebook.parent / 'report.html'
        report.write_text('<!doctype html><html><body><main style="display:grid;grid-template-columns:1fr 1fr">' +
                          fragment.read_text() + '<aside><h2>Results</h2><p>Description on the right.</p></aside>' +
                          '</main></body></html>')
        page.evaluate('path => openFile(path)', str(report))
        preview = page.frame_locator('#html-preview-frame')
        plot = preview.frame_locator('iframe.gusnb-viz-frame')
        expect(plot.locator('svg .bar')).to_have_count(3)
        expect(preview.locator('aside')).to_contain_text('Description on the right.')
        expect(preview.locator('iframe.gusnb-viz-frame')).to_have_attribute('sandbox', 'allow-scripts')
        preview.get_by_role('button', name='Notebook code', exact=True).click()
        page.wait_for_function('args => active === args.path && selected === args.id',
                               arg={'path': str(notebook), 'id': cell_id})
        assert 'd3.select' in page.evaluate('cells[0].source')
        assert 'display(HTML(' in page.evaluate('cells[0].source')
        page.wait_for_function('''async args => {
          const h = await api(`/api/cells/${args.id}/history?${new URLSearchParams({notebook:args.path})}`);
          return h.events.some(e => e.kind === 'export' && e.destination === args.report);
        }''', arg={'id': cell_id, 'path': str(notebook), 'report': str(report)})
        deadline = time.monotonic() + 5
        while True:
            saved = nbformat.read(notebook, 4)
            receipts = saved.cells[0].metadata['gusnotebook_history']
            if any(event['kind'] == 'export' for event in receipts) or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(100)
        assert any(event['kind'] == 'copy' for event in receipts)
        assert any(event['kind'] == 'export' for event in receipts)

        # An edit without execution must not mislabel the previous output.
        page.evaluate('async args => api(`/api/cells/${args.id}?${new URLSearchParams({notebook:args.path})}`, {method:"PATCH", body:JSON.stringify({source:"print(42)"})})',
                      {'id': cell_id, 'path': str(notebook)})
        stale = subprocess.run(cli + ['export-output', cell_id, '--file', str(fragment)],
                               capture_output=True, text=True, timeout=30)
        assert stale.returncode != 0 and 'Run this cell' in stale.stderr, stale
        exported_frame = fragment.read_text()
        assert 'd3.select' in exported_frame
        print('PASS: terminal export embeds executed D3 in custom HTML; code link and durable history work; stale code is rejected', flush=True)

        # Frames produced by the existing copy/export path carry D3 inline too.
        exported = page.evaluate('source => provenanceRenderHtml({mime: "text/html", source}, "d3-export")', source)
        context.set_offline(True)
        copy = context.new_page()
        copy.on('pageerror', lambda error: errors.append(str(error)))
        copy.set_content(exported)
        expect(copy.frame_locator('iframe').locator('svg .bar')).to_have_count(3)
        expect(copy.frame_locator('iframe').locator('script[src]')).to_have_count(0)
        cli_copy = context.new_page()
        cli_copy.set_content(exported_frame)
        expect(cli_copy.frame_locator('iframe').locator('svg .bar')).to_have_count(3)
        assert not external, external
        assert not errors, errors
        browser.close()
        print('PASS: saved output reloads; exported figure renders with networking disabled', flush=True)


if __name__ == '__main__':
    main()
