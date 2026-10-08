"""PDF previews open from Files, survive reloads, and offer the original download."""

import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def multipage_pdf():
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for page in range(3):
        stream = f'BT /F1 24 Tf 72 700 Td (PDF page {page + 1}) Tj ET'.encode()
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + page * 2} 0 R >>'.encode(),
                    f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream']
    result, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(result)
    result += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    result += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    return result + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']).resolve() / 'work'
    pdf = work / 'report # & α.PDF'
    content = multipage_pdf()
    pdf.write_bytes(content)
    second = work / 'second.pdf'
    second.write_bytes(content)
    with sync_playwright() as playwright:
        # Native PDF support needs full Chromium, not its minimal headless shell.
        browser = (playwright.chromium.launch(channel='chromium', headless=True)
                   if not os.environ.get('GUSNOTEBOOK_BROWSER_CHANNEL')
                   and Path(playwright.chromium.executable_path).exists()
                   else launch_browser(playwright))
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors = []
        pdf_requests = []
        page.on('request', lambda request: pdf_requests.append(request.url) if '/api/pdf?' in request.url else None)
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
        assert parse_qs(urlsplit(viewer.get_attribute('src')).query)['path'] == [str(pdf)]
        assert viewer.bounding_box()['height'] > 600
        assert page.evaluate('navigator.pdfViewerEnabled')
        pdf_frame = native_viewer.value
        expect(pdf_frame.get_by_role('textbox', name='Page number', exact=True)).to_have_value('1')
        expect(pdf_frame.get_by_role('tab', name='Thumbnail for page 1', exact=True)).to_be_visible()
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-pdf.png'))
        with page.expect_download() as download:
            page.click('#pdf-download')
        assert Path(download.value.path()).read_bytes() == content
        pdf_frame.evaluate('window.gusnbRetainedViewer = true')
        page_number = pdf_frame.get_by_role('textbox', name='Page number', exact=True)
        page_number.fill('2'); page_number.press('Enter')
        zoom_level = pdf_frame.get_by_role('textbox', name='Zoom level', exact=True)
        zoom_level.fill('150'); zoom_level.press('Enter')
        expect(page_number).to_have_value('2')
        expect(zoom_level).to_have_value('150%')
        loaded_requests = len(pdf_requests)
        page.evaluate('path => switchTab(path)', notebook)
        expect(page.locator('#pdfpane')).not_to_be_visible()
        page.evaluate('path => switchTab(path)', str(pdf))
        expect(page.locator('#pdfpane')).to_be_visible()
        assert not pdf_frame.is_detached(), 'Switching tabs destroyed the PDF viewer'
        assert pdf_frame.evaluate('window.gusnbRetainedViewer === true')
        expect(page_number).to_have_value('2')
        expect(zoom_level).to_have_value('150%')
        assert len(pdf_requests) == loaded_requests
        with page.expect_event('framenavigated', predicate=lambda frame: frame.url.startswith('chrome-extension://')) as second_viewer:
            page.evaluate('path => openFile(path)', str(second))
        second_frame = second_viewer.value
        second_page = second_frame.get_by_role('textbox', name='Page number', exact=True)
        expect(second_page).to_have_value('1')
        second_page.fill('3'); second_page.press('Enter')
        expect(second_page).to_have_value('3')
        expect(page.locator('.pdf-viewer')).to_have_count(2)
        loaded_requests = len(pdf_requests)
        for _ in range(2):
            page.evaluate('path => switchTab(path)', str(pdf))
            expect(page_number).to_have_value('2')
            expect(zoom_level).to_have_value('150%')
            page.evaluate('path => switchTab(path)', str(second))
            expect(second_page).to_have_value('3')
        assert len(pdf_requests) == loaded_requests, 'Tab switches requested the PDFs again'
        page.evaluate('path => switchTab(path)', str(pdf))
        page.evaluate('path => closeTab(path)', str(second))
        expect(page.locator('.pdf-viewer')).to_have_count(1)
        assert second_frame.is_detached()
        expect(page_number).to_have_value('2')
        print('PASS: PDF/notebook and PDF/PDF switches preserve native page and zoom without fetching again; closing releases the viewer', flush=True)
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
        expect(page.locator('.pdf-viewer')).to_have_count(0)
        assert pdf.read_bytes() == content
        assert not errors, errors
        print('PASS: PDF preview, reload, download, tab switching, session restore, and narrow layouts', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
