"""Large HTML with an embedded figure opens, edits, saves and reloads."""

import base64
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="24"><rect width="32" height="24" fill="teal"/><!--' + 'x' * (2 * 1024 * 1024) + '--></svg>'
    image = 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()
    document = '<!doctype html><html><body><h1>Large report</h1><p id="intro">Original report</p><figure><img id="figure" alt="Embedded figure" src="' + image + '"><figcaption>Figure one</figcaption></figure></body></html>'
    report = work / 'large report.HTML'
    report.write_text(document)
    assert report.stat().st_size > 2 * 1024 * 1024
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('path => openFile(path)', str(report))
        frame = page.frame_locator('#html-preview-frame')
        expect(frame.locator('#intro')).to_have_text('Original report')
        expect(frame.locator('[data-gusnotebook-runtime="authoring"]')).to_be_attached()
        frame.locator('#figure').evaluate('el => el.decode()')
        assert frame.locator('#figure').evaluate('el => el.naturalWidth') == 32
        frame.locator('#intro').evaluate('''el => {
          window.focus(); document.body.focus({preventScroll:true});
          const range = document.createRange(); range.selectNodeContents(el);
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
          el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
        }''')
        page.wait_for_function('reportedMarkupPath === active')
        page.keyboard.insert_text('Edited large report')
        expect(frame.locator('#intro')).to_have_text('Edited large report')
        page.wait_for_function('activeTab().text.includes("Edited large report")')
        page.click('#text-save')
        page.wait_for_function('!activeTab().dirty && !activeTab().saveInFlight && !activeTab().markupSavePending')
        saved = report.read_text()
        assert 'Edited large report' in saved
        assert image in saved
        assert 'data-gusnotebook-runtime=' not in saved
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('booted')
        expect(frame.locator('#intro')).to_have_text('Edited large report')
        frame.locator('#figure').evaluate('el => el.decode()')
        assert not errors, errors
        print('PASS: HTML over 2 MB opens, renders an embedded figure, supports visual selection, edits, saves and reloads', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
