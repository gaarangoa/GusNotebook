"""Real D3 notebook figures and exported frames render without external assets."""

import html as html_lib
import json
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
        # Older exports contain the whole source in a popup. They must use the
        # same path-only inspector without needing to regenerate the report.
        legacy = fragment.read_text().replace(html_lib.escape(str(notebook)) + '</pre>',
                                              html_lib.escape(str(notebook) + '\n\n' + code) + '</pre>')
        report.write_text('<!doctype html><html><head><style>body{margin:40px;font:14px system-ui}'
                          'button{font-size:32px;background:red}</style></head><body>'
                          '<h1>Analysis results</h1><main style="display:grid;grid-template-columns:1fr 1fr">' +
                          legacy + '<aside><h2>Results</h2><p>Description on the right.</p></aside>' +
                          '</main></body></html>')
        page.evaluate('path => openFile(path)', str(report))
        preview = page.frame_locator('#html-preview-frame')
        plot = preview.frame_locator('iframe.gusnb-viz-frame')
        expect(plot.locator('svg .bar')).to_have_count(3)
        expect(preview.locator('aside')).to_contain_text('Description on the right.')
        expect(preview.locator('iframe.gusnb-viz-frame')).to_have_attribute('sandbox', 'allow-scripts')
        plot.locator('svg').click(position={'x':10, 'y':10})
        controls = preview.locator('[data-gusnotebook-runtime="viz-controls"]')
        toolbar = controls.get_by_role('toolbar', name='Plot controls')
        source_button = toolbar.get_by_role('button', name='Source', exact=True)
        source_button.click()
        assert not page.evaluate('activeTab().dirty'), 'Inspecting a plot must not change its layout'
        assert preview.locator('.gusnb-viz').get_attribute('data-gusnb-free-placement') is None
        inspector = controls.get_by_role('dialog', name='Plot source')
        expect(inspector).to_be_visible()
        expect(inspector.locator('#source-path')).to_have_text(str(notebook))
        expect(inspector).not_to_contain_text('d3.select')
        expect(preview.locator('[data-gusnb-viz-panel]')).not_to_be_visible()
        expect(source_button).to_have_css('font-size', f"{page.evaluate('AppAppearance.get().fontSize')}px")
        for theme in ('light', 'dark'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            if theme == 'dark':
                expect(controls).to_have_attribute('data-dark', '')
                expect(inspector).to_have_css('background-color', 'rgb(34, 40, 50)')
            else:
                expect(controls).not_to_have_attribute('data-dark', '')
                expect(inspector).to_have_css('background-color', 'rgb(255, 255, 255)')
            page.screenshot(path=str(Path(tempfile.gettempdir()) / f'gusnb-plot-source-{theme}.png'))
        page.set_viewport_size({'width':680, 'height':760})
        preview.locator('[data-gusnotebook-runtime="viz-controls"]').evaluate('''host => {
          const panel=host.shadowRoot.getElementById('source'), rect=panel.getBoundingClientRect();
          if (rect.left < 0 || rect.right > innerWidth || rect.bottom > innerHeight) throw Error('Source card clipped');
        }''')
        page.set_viewport_size({'width':1440, 'height':960})
        inspector.get_by_role('button', name='Close source').press('Escape')
        expect(inspector).not_to_be_visible()
        expect(source_button).to_have_attribute('aria-expanded', 'false')
        source_button.click()
        inspector.get_by_role('button', name='Open notebook code').click()
        page.wait_for_function('args => active === args.path && selected === args.id',
                               arg={'path': str(notebook), 'id': cell_id})
        page.evaluate('path => switchTab(path)', str(report))
        expect(plot.locator('svg .bar')).to_have_count(3)
        # The original link also works after selecting the figure: a click on
        # it must not start dragging instead of opening the notebook.
        preview.locator('.gusnb-viz').evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
        toolbar.get_by_role('button', name='Increase width', exact=True).click()
        assert preview.locator('.gusnb-viz').evaluate('el => parseFloat(el.style.width) > 180')
        box = plot.locator('svg').bounding_box()
        page.mouse.move(box['x']+45, box['y']+30)
        page.mouse.down()
        page.mouse.move(box['x']+85, box['y']+50, steps=5)
        page.mouse.up()
        expect(preview.locator('.gusnb-viz')).to_have_attribute('data-gusnb-free-placement', '1')
        assert preview.locator('.gusnb-viz').evaluate('el => parseFloat(el.style.left) >= 30')
        page.evaluate('saveText()')
        page.wait_for_function('!activeTab().dirty && !activeTab().saveInFlight')
        saved_report = report.read_text()
        assert 'data-gusnotebook-runtime' not in saved_report
        assert 'source-open' not in saved_report
        payload = preview.locator('[data-gusnb-viz-source]').text_content()
        assert json.loads(payload)['code'] == code
        preview.get_by_role('button', name='Notebook code', exact=True).click()
        page.wait_for_function('args => active === args.path && selected === args.id',
                               arg={'path': str(notebook), 'id': cell_id})
        assert 'd3.select' in page.evaluate('cells[0].source')
        assert 'display(HTML(' in page.evaluate('cells[0].source')
        # The clipboard exporter uses the same path-only display, while its
        # embedded provenance continues to retain the source for auditing.
        copied = page.evaluate('''() => {
          const result=provenanceHtml(cells[0], null, {});
          const dom=new DOMParser().parseFromString(result.html,'text/html');
          return {path:dom.querySelector('[data-gusnb-viz-panel]').textContent,
            code:JSON.parse(dom.querySelector('[data-gusnb-viz-source]').textContent).code};
        }''')
        assert copied['path'] == str(notebook) and copied['code'] == code
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
