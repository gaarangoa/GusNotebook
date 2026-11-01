"""Oversized centered pages fit, recenter and stay navigable without edits."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        frame = page.frame_locator('#html-preview-frame')
        canvas = page.locator('#html-view-viewport')

        def fit(name):
            page.get_by_role('button', name=name, exact=True).click()
            page.wait_for_function('activeTab().markupFitRequest === null')

        def assert_visible():
            view = canvas.bounding_box()
            content = frame.locator('main').bounding_box()
            assert content['x'] >= view['x'] - 1, (view, content)
            assert content['y'] >= view['y'] - 1, (view, content)
            assert content['x'] + content['width'] <= view['x'] + view['width'] + 1, (view, content)
            assert content['y'] + content['height'] <= view['y'] + view['height'] + 1, (view, content)
            for axis, dimension in [('x', 'width'), ('y', 'height')]:
                assert abs(content[axis] + content[dimension] / 2 - view[axis] - view[dimension] / 2) < 2
            # Bounding boxes alone can include content clipped by the iframe.
            assert frame.locator('main').evaluate('''el => {
              const r=el.getBoundingClientRect();
              return r.left >= -1 && r.top >= -1 && r.right <= innerWidth+1 && r.bottom <= innerHeight+1;
            }''')

        for width, height in [(2400, 1800), (24000, 1800), (2400, 16000)]:
            report = work / f'centered-{width}-{height}.html'
            original = f'''<!doctype html><html><head><style>
html,body{{margin:0;width:100%;height:100%;overflow:hidden}}
body{{display:flex;align-items:center;justify-content:center}}
main{{flex:none;width:{width}px;height:{height}px;position:relative;background:linear-gradient(135deg,#e2c5ff,#bae7ff)}}
h1{{margin:0}}button{{position:absolute;right:16px;bottom:16px}}
</style></head><body><main><h1>Whole report</h1>
<button onclick="this.textContent='Clicked'">Corner action</button></main></body></html>'''
            report.write_text(original)
            page.evaluate('path => openFile(path)', str(report))
            expect(frame.locator('h1')).to_have_text('Whole report')
            expect(frame.locator('[data-gusnotebook-runtime="authoring"]')).to_be_attached()
            fit('Fit width')
            size = page.evaluate('activeTab().markupCanvasSize')
            assert size['width'] == width and size['height'] == height, size
            if width == 24000:
                assert page.evaluate('activeTab().markupZoom') < .1
            fit('Fit page')
            assert_visible()
            assert report.read_text() == original and page.evaluate('!activeTab().dirty')

            # At 100%, retain the full measured viewport and pan over it.
            page.get_by_role('button', name='Reset zoom to 100%', exact=True).click()
            assert frame.locator('main').evaluate('el => el.getBoundingClientRect().left') >= 0
            page.get_by_role('button', name='Pan', exact=True).click()
            surface = page.locator('#html-pan-surface')
            box = surface.bounding_box()
            page.mouse.move(box['x'] + box['width'] * .8, box['y'] + box['height'] * .8)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] * .2, box['y'] + box['height'] * .2, steps=8)
            page.mouse.up()
            assert canvas.evaluate('el => el.scrollLeft > 0 && el.scrollTop > 0')
            surface.press('Escape')
            fit('Fit page')
            assert_visible()
            assert canvas.evaluate('el => [el.scrollLeft, el.scrollTop]') == [0, 0]
            if width == 2400 and height == 1800:
                frame.get_by_role('button', name='Corner action').click()
                expect(frame.get_by_role('button', name='Clicked')).to_be_visible()
                page.screenshot(path='/tmp/gusnb-markup-fit.png')
            assert report.read_text() == original and page.evaluate('!activeTab().dirty')
            print(f'PASS: {width}×{height} centered report fits, pans and recenters without changing the file', flush=True)
        assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
