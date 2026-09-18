"""PDF previews open from Files, survive reloads, and offer the original download."""

import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']).resolve() / 'work'
    pdf = work / 'report # & α.PDF'
    content = (Path(__file__).parent / 'fixtures/sample.pdf').read_bytes()
    pdf.write_bytes(content)
    with sync_playwright() as playwright:
        # Native PDF support needs full Chromium, not its minimal headless shell.
        browser = (playwright.chromium.launch(channel='chromium', headless=True)
                   if not os.environ.get('GUSNOTEBOOK_BROWSER_CHANNEL')
                   and Path(playwright.chromium.executable_path).exists()
                   else launch_browser(playwright))
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        notebook = page.evaluate('active')
        with page.expect_event('framenavigated', predicate=lambda frame: frame.url.startswith('chrome-extension://')) as native_viewer:
            with page.expect_response(lambda response: '/api/pdf?' in response.url and response.ok):
                page.locator('.file-row').filter(has_text=pdf.name).click()
        expect(page.locator('#pdfpane')).to_be_visible()
        expect(page.locator('#textpane')).not_to_be_visible()
        expect(page.locator('#toolbar')).not_to_be_visible()
        viewer = page.locator('#pdf-viewer')
        expect(viewer).to_be_visible()
        assert page.evaluate('activeTab().kind') == 'pdf'
        assert parse_qs(urlsplit(viewer.get_attribute('data')).query)['path'] == [str(pdf)]
        assert viewer.bounding_box()['height'] > 600
        assert page.evaluate('navigator.pdfViewerEnabled')
        pdf_frame = native_viewer.value
        expect(pdf_frame.get_by_role('textbox', name='Page number', exact=True)).to_have_value('1')
        expect(pdf_frame.get_by_role('tab', name='Thumbnail for page 1', exact=True)).to_be_visible()
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-pdf.png'))
        with page.expect_download() as download:
            page.click('#pdf-download')
        assert Path(download.value.path()).read_bytes() == content
        with page.expect_response(lambda response: '/api/pdf?' in response.url and 'v=' in response.url and response.ok):
            page.locator('#pdfpane').get_by_role('button', name='Reload').click()
        page.evaluate('path => switchTab(path)', notebook)
        expect(page.locator('#pdfpane')).not_to_be_visible()
        with page.expect_response(lambda response: '/api/sessions/' in response.url
                                  and response.request.method == 'POST'
                                  and response.request.post_data_json.get('active') == str(pdf)
                                  and response.ok):
            page.evaluate('path => switchTab(path)', str(pdf))
        expect(page.locator('#pdfpane')).to_be_visible()
        page.reload(wait_until='domcontentloaded')
        expect(page.locator('#pdfpane')).to_be_visible()
        assert page.evaluate('activeTab().kind') == 'pdf'
        for width in (768, 390):
            page.set_viewport_size({'width': width, 'height': 900})
            expect(page.locator('#pdf-download')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.evaluate('path => closeTab(path)', str(pdf))
        expect(page.locator('#pdfpane')).not_to_be_visible()
        assert pdf.read_bytes() == content
        assert not errors, errors
        browser.close()
        print('PASS: PDF preview, reload, download, tab switching, session restore, and narrow layouts', flush=True)


if __name__ == '__main__':
    main()
