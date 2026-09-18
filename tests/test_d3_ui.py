"""Real D3 notebook figures and exported frames render without external assets."""

import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import nbformat
from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated

from gusnotebook.plotting import D3_EXAMPLE
from gusnotebook.skills import code_of


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    notebook = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work' / 'd3.ipynb'
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
            if request.request.url.startswith(url):
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

        # Frames produced by the existing copy/export path carry D3 inline too.
        exported = page.evaluate('source => provenanceRenderHtml({mime: "text/html", source}, "d3-export")', source)
        context.set_offline(True)
        copy = context.new_page()
        copy.on('pageerror', lambda error: errors.append(str(error)))
        copy.set_content(exported)
        expect(copy.frame_locator('iframe').locator('svg .bar')).to_have_count(3)
        expect(copy.frame_locator('iframe').locator('script[src]')).to_have_count(0)
        assert not external, external
        assert not errors, errors
        browser.close()
        print('PASS: saved output reloads; exported figure renders with networking disabled', flush=True)


if __name__ == '__main__':
    main()
