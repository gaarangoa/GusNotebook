"""Zoom scales responsive page contents without changing their layout viewport."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']).resolve() / 'work'
    report = work / 'responsive.html'
    original = '''<!doctype html><html><head><style>
html,body{margin:0}main{width:100vw;height:100vh;background:#edf5fc}
h1{font:6vw/1.2 system-ui;margin:0}img{width:60%;display:block}
iframe{width:60vw;height:20vh;border:0}button{position:absolute;left:70vw;top:70vh}
</style></head><body><main><h1>Responsive report</h1>
<img alt="Responsive diagram" src="diagram.svg">
<iframe id="nested" srcdoc="<style>body{margin:0;font:5vw system-ui}</style><p>Nested chart</p>"></iframe>
<button id="corner" onclick="this.textContent='Clicked'">Corner action</button>
</main></body></html>'''
    report.write_text(original)
    (work / 'diagram.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200" viewBox="0 0 600 200"><rect width="600" height="200" fill="#2978a0"/></svg>')
    other = work / 'other.html'
    other.write_text('<h1>Another document</h1>')

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width':1440, 'height':1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('path => openFile(path)', str(report))
        frame = page.frame_locator('#html-preview-frame')
        expect(frame.locator('h1')).to_have_text('Responsive report')
        expect(frame.locator('[data-gusnotebook-runtime="authoring"]')).to_be_attached()
        items = [frame.locator('h1'), frame.locator('img'), frame.locator('#nested'),
                 frame.frame_locator('#nested').locator('p')]
        sizes = [item.bounding_box() for item in items]
        viewport = frame.locator('body').evaluate('() => [innerWidth, innerHeight]')
        for factor in (.5, 2):
            page.evaluate('factor => setMarkupZoom(factor)', factor)
            expect(page.locator('#html-zoom-reset')).to_have_text(f'{round(factor * 100)}%')
            for item, before in zip(items, sizes):
                after = item.bounding_box()
                for dimension in ('width', 'height'):
                    assert abs(after[dimension] - before[dimension] * factor) < 2, (factor, dimension, before, after)
            assert frame.locator('body').evaluate('() => [innerWidth, innerHeight]') == viewport
        assert report.read_text() == original and page.evaluate('!activeTab().dirty')
        print('PASS: text, responsive images, and nested frames visually scale without reflowing the document', flush=True)

        page.get_by_role('button', name='Pan', exact=True).click()
        surface = page.locator('#html-pan-surface')
        box = surface.bounding_box()
        page.mouse.move(box['x'] + box['width']*.8, box['y'] + box['height']*.8)
        page.mouse.down()
        page.mouse.move(box['x'] + box['width']*.2, box['y'] + box['height']*.2, steps=10)
        page.mouse.up()
        canvas = page.locator('#html-view-viewport')
        page.wait_for_function("document.getElementById('html-view-viewport').scrollLeft > 100 && document.getElementById('html-view-viewport').scrollTop > 100")
        saved_scroll = canvas.evaluate('el => [el.scrollLeft, el.scrollTop]')
        surface.press('Escape')
        frame.locator('#corner').click()
        expect(frame.locator('#corner')).to_have_text('Clicked')
        page.evaluate('path => openFile(path)', str(other))
        expect(frame.locator('h1')).to_have_text('Another document')
        page.evaluate('path => switchTab(path)', str(report))
        expect(frame.locator('h1')).to_have_text('Responsive report')
        expect(page.locator('#html-zoom-reset')).to_have_text('200%')
        assert canvas.evaluate('el => [el.scrollLeft, el.scrollTop]') == saved_scroll
        page.get_by_role('button', name='Reset zoom to 100%', exact=True).click()
        expect(page.locator('#html-zoom-reset')).to_have_text('100%')
        assert canvas.evaluate('el => [el.scrollLeft, el.scrollTop]') == [0, 0]
        assert not errors, errors
        print('PASS: magnified pages pan, remain interactive, remember their position, and reset cleanly', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
