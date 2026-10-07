"""Image dragging and proportional resizing in HTML, with undo and clean saves."""

import base64
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']).resolve() / 'work'
    report = work / 'images.html'
    report.write_text('''<!doctype html><html><head><style>
body{margin:32px;font:16px system-ui;background:white;color:#243244}
main{position:relative;width:900px;min-height:1800px}
img{display:block;width:240px;max-width:100%;height:auto;margin:12px 0}
#absolute{position:absolute;right:30px;top:700px;width:180px}
</style></head><body><main><h1>Image layout</h1><p>Figures can be placed beside the analysis.</p>
<img id="plot" src="plot.png" alt="Study results"><p id="after">Text after the figure.</p>
<img id="absolute" src="plot.png" alt="Positioned image">
<button id="interactive" onclick="this.textContent='Still works'">Chart action</button>
<div id="live">0</div>
<script>window.liveTick=0;setInterval(()=>{document.getElementById('live').textContent=++window.liveTick},20)</script>
</main></body></html>''')

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width':1440, 'height':1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        png = base64.b64decode(page.evaluate('''() => {
          const canvas=document.createElement('canvas');canvas.width=1200;canvas.height=600;
          const ctx=canvas.getContext('2d');ctx.fillStyle='#d7e8f2';ctx.fillRect(0,0,1200,600);
          ctx.fillStyle='#28729a';ctx.fillRect(100,200,180,300);ctx.fillRect(400,100,180,400);
          ctx.fillRect(700,260,180,240);return canvas.toDataURL('image/png').split(',')[1];
        }'''))
        asset = work / 'plot.png'
        asset.write_bytes(png)
        page.evaluate('path => openFile(path)', str(report))
        frame = page.frame_locator('#html-preview-frame')
        image = frame.locator('#plot')
        tools = frame.locator('[data-gusnotebook-runtime="authoring"]')
        expect(tools).to_be_attached()
        expect(image).to_have_css('height', '120px')
        image.click()
        mover = tools.get_by_role('button', name='Move image', exact=True)
        resize = tools.get_by_role('button', name='Resize image from bottom right')
        toolbar = tools.get_by_role('toolbar', name='Image size')
        expect(mover).to_be_visible()
        expect(resize).to_be_visible()
        assert page.evaluate('!activeTab().dirty')

        def geometry(locator=image):
            return locator.evaluate('el => {const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}}')

        def drag(control, dx, dy, cancel=False):
            box = control.bounding_box()
            x, y = box['x'] + box['width']/2, box['y'] + box['height']/2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + dx, y + dy, steps=12)
            if cancel:
                page.keyboard.press('Escape')
            page.mouse.up()

        def near(actual, expected):
            assert abs(actual - expected) <= 1.5, (actual, expected)

        initial = geometry()
        after_y = geometry(frame.locator('#after'))['y']
        drag(mover, 150, 80)
        moved = geometry()
        near(moved['x'], initial['x'] + 150)
        near(moved['y'], initial['y'] + 80)
        near(geometry(frame.locator('#after'))['y'], after_y)
        assert frame.locator('#live').inner_text() != '0'
        page.keyboard.press('ControlOrMeta+z')
        near(geometry()['x'], initial['x'])
        near(geometry()['y'], initial['y'])
        page.keyboard.press('ControlOrMeta+Shift+z')
        near(geometry()['x'], moved['x'])
        near(geometry()['y'], moved['y'])
        drag(mover, 60, 30, cancel=True)
        near(geometry()['x'], moved['x'])
        near(geometry()['y'], moved['y'])
        print('PASS: images drag without shifting surrounding text; one undo restores a drag and Escape cancels', flush=True)

        drag(resize, 120, 60)
        near(geometry()['width'], 360)
        near(geometry()['height'], 180)
        toolbar.get_by_role('button', name='Undo', exact=True).click()
        near(geometry()['width'], 240)
        toolbar.get_by_role('button', name='Redo', exact=True).click()
        near(geometry()['width'], 360)
        # The opposite corner stays anchored when resizing from the top left.
        previous = geometry()
        drag(tools.get_by_role('button', name='Resize image from top left'), -60, -30)
        current = geometry()
        near(current['width'], 420)
        near(current['x'] + current['width'], previous['x'] + previous['width'])
        near(current['y'] + current['height'], previous['y'] + previous['height'])
        tools.get_by_label('Image width', exact=True).fill('320')
        tools.get_by_label('Image width', exact=True).press('Enter')
        near(geometry()['width'], 320)
        near(geometry()['height'], 160)
        print('PASS: corner resizing preserves proportions and anchors; width controls and undo still work', flush=True)

        # Pointer coordinates must remain correct with the viewer zoomed out.
        page.evaluate('setMarkupZoom(.5)')
        previous = geometry()
        drag(mover, 40, 25)
        near(geometry()['x'], previous['x'] + 80)
        near(geometry()['y'], previous['y'] + 50)
        drag(resize, 40, 20)
        near(geometry()['width'], 400)
        near(geometry()['height'], 200)
        mover.focus()
        previous = geometry()
        mover.press('Shift+ArrowRight')
        near(geometry()['x'], previous['x'] + 10)
        mover.press('ArrowDown')
        near(geometry()['y'], previous['y'] + 1)

        positioned = frame.locator('#absolute')
        positioned.click()
        previous = geometry(positioned)
        drag(mover, -35, -20)
        near(geometry(positioned)['x'], previous['x'] - 70)
        near(geometry(positioned)['y'], previous['y'] - 40)
        toolbar.get_by_role('button', name='Undo', exact=True).click()
        near(geometry(positioned)['x'], previous['x'])
        expect(positioned).to_have_css('right', '30px')
        frame.locator('#interactive').click()
        expect(frame.locator('#interactive')).to_have_text('Still works')
        expect(mover).not_to_be_visible()
        print('PASS: zoomed dragging, keyboard movement, and existing positioned images retain correct coordinates', flush=True)

        frame.locator('#after').evaluate('''async el => {
          el.focus();const range=document.createRange();range.selectNodeContents(el);range.collapse(false);
          const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
          const blob=await (await fetch('plot.png')).blob();
          const clipboard=new DataTransfer();clipboard.items.add(new File([blob],'pasted.png',{type:'image/png'}));
          el.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:clipboard}));
        }''')
        pasted = frame.locator('img[src^="pasted-image-"]')
        expect(pasted).to_have_count(1)
        expect(pasted).to_have_css('width', '240px')
        previous = geometry(pasted)
        drag(mover, 60, 35)
        near(geometry(pasted)['x'], previous['x'] + 120)
        near(geometry(pasted)['y'], previous['y'] + 70)
        drag(resize, 40, 20)
        near(geometry(pasted)['width'], 320)
        near(geometry(pasted)['height'], 160)
        assert len(list(work.glob('pasted-image-*.png'))) == 1
        print('PASS: newly pasted PNGs have the same drag and resize controls as existing images', flush=True)

        final = geometry()
        page.click('#text-save')
        page.wait_for_function('!activeTab().dirty && !activeTab().saveInFlight')
        saved = report.read_text()
        assert 'data-gusnotebook-runtime' not in saved and 'image-handle' not in saved
        assert 'contenteditable="true"' not in saved
        assert 'plot.png' in saved and asset.read_bytes() == png
        page.click('#text-reload')
        expect(image).to_have_css('width', '400px')
        for key, value in final.items():
            near(geometry()[key], value)
        image.click()
        for theme in ('dark', 'light'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            expect(resize).to_be_visible()
            page.screenshot(path=str(Path(tempfile.gettempdir()) / f'gusnb-html-images-{theme}.png'))
        assert not errors, errors
        print('PASS: image layout survives save/reload with original assets, clean HTML, and theme-aware handles', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
