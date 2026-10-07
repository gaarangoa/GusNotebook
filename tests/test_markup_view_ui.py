"""Large HTML/SVG navigation stays separate from document editing and saves."""

import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    work = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]).resolve() / "work"
    report = work / "large.html"
    original = '''<!doctype html><html><head><style>
html, body {margin:0; overflow:hidden}
body {width:2400px;height:1800px;background:#fff;color:#17212f;font:24px sans-serif}
h1 {font-size:80px;margin:0}
#far {position:absolute;left:2150px;top:1300px;width:200px}
#scroller {position:absolute;left:50px;top:260px;width:400px;height:220px;overflow:auto}
#inside {width:1200px;height:800px;background:linear-gradient(135deg,#ddd,#6bc)}
</style></head><body><h1>Large report</h1><p id="editable">Original text</p>
<div id="scroller"><div id="inside">Scrollable chart</div></div>
<p id="far">Far corner</p></body></html>'''
    report.write_text(original)
    other = work / "other.html"
    other.write_text('<!doctype html><html><body><h1>Other report</h1></body></html>')
    svg = work / "large.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="2400" height="1800">'
                   '<text x="100" y="200" font-size="80">Large SVG</text></svg>')

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width":1440, "height":1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("booted")
        notebook = page.evaluate("active")
        page.evaluate("path => openFile(path)", str(report))
        frame = page.frame_locator("#html-preview-frame")
        expect(frame.locator('h1')).to_have_text("Large report")
        expect(frame.locator('[data-gusnotebook-runtime="authoring"]')).to_be_attached()
        controls = page.get_by_role("group", name="Document view", exact=True)
        zoom = page.locator("#html-zoom-reset")
        surface = page.locator("#html-pan-surface")

        def window_eval(expression):
            return frame.locator("body").evaluate("el => " + expression)

        def drag(dx, dy, x=None, y=None):
            box = surface.bounding_box()
            x = box["x"] + (box["width"] * .75 if x is None else x)
            y = box["y"] + (box["height"] * .7 if y is None else y)
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x - dx, y - dy, steps=8)
            page.mouse.up()

        expect(zoom).to_have_text("100%")
        controls.get_by_role("button", name="Zoom out", exact=True).click()
        expect(zoom).to_have_text("90%")
        # The document keeps its authored styles while the entire view scales.
        expect(frame.locator('h1')).to_have_css("font-size", "80px")
        controls.get_by_role("button", name="Zoom in", exact=True).click()
        expect(zoom).to_have_text("100%")
        controls.get_by_role("button", name="Fit width", exact=True).click()
        page.wait_for_function("activeTab().markupZoom < .7 && activeTab().markupZoom > .1")
        fit_zoom = page.evaluate("activeTab().markupZoom")
        assert window_eval("innerWidth") >= 2400
        assert page.locator('#html-preview-frame').bounding_box()['width'] <= page.locator('#html-preview').bounding_box()['width'] + 1
        page.evaluate("setMarkupZoom(.1)")
        controls.get_by_role("button", name="Fit width", exact=True).click()
        page.wait_for_function("expected => Math.abs(activeTab().markupZoom - expected) < .002", arg=fit_zoom)
        assert page.evaluate("!activeTab().dirty")
        assert report.read_text() == original
        print("PASS: zoom and fit width reveal oversized HTML without altering its styles or source", flush=True)

        zoom.click()
        controls.get_by_role("button", name="Pan", exact=True).click()
        expect(surface).to_be_visible()
        drag(180, 120)
        page.wait_for_function("activeTab().markupView?.x >= 170 && activeTab().markupView?.y >= 110")
        assert window_eval("scrollX") >= 170 and window_eval("scrollY") >= 110
        # Even overflow:hidden pages can be moved; keyboard and wheel also work.
        surface.press("ArrowDown")
        page.wait_for_function("activeTab().markupView?.y >= 190")
        page.mouse.wheel(0, 100)
        page.wait_for_function("activeTab().markupView?.y >= 280")
        page.evaluate("markupCommand('pan-view', {root:true, dx:-10000, dy:-10000})")
        page.wait_for_function("activeTab().markupView?.x === 0 && activeTab().markupView?.y === 0")
        drag(100, 70, x=300, y=400)
        expect(frame.locator('#scroller')).to_have_js_property('scrollLeft', 100)
        expect(frame.locator('#scroller')).to_have_js_property('scrollTop', 70)
        assert window_eval("scrollX") == 0 and window_eval("scrollY") == 0
        assert page.evaluate("!activeTab().dirty")
        surface.press("Escape")
        expect(surface).not_to_be_visible()
        expect(page.locator('#html-pan-toggle')).to_have_attribute('aria-pressed', 'false')
        print("PASS: dragging, keyboard, and wheel pan the document and nested scrollers without editing", flush=True)

        controls.get_by_role("button", name="Zoom out", exact=True).click()
        frame.locator('#editable').click()
        frame.locator('#editable').evaluate("""el => {
          const range = document.createRange(); range.selectNodeContents(el);
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }""")
        page.keyboard.insert_text("Edited while zoomed")
        expect(frame.locator('#editable')).to_have_text('Edited while zoomed')
        page.click('#text-save')
        page.wait_for_function("!activeTab().dirty && !activeTab().saveInFlight")
        saved = report.read_text()
        assert 'Edited while zoomed' in saved
        assert '--markup-zoom' not in saved and 'html-pan-surface' not in saved
        assert 'data-gusnotebook-runtime' not in saved
        controls.get_by_role("button", name="Pan", exact=True).click()
        page.evaluate("path => openFile(path)", str(other))
        expect(frame.locator('h1')).to_have_text('Other report')
        expect(zoom).to_have_text('100%')
        expect(surface).not_to_be_visible()
        page.evaluate("path => switchTab(path)", str(report))
        expect(frame.locator('#editable')).to_have_text('Edited while zoomed')
        expect(zoom).to_have_text('90%')
        expect(surface).to_be_visible()
        page.click('#text-reload')
        expect(frame.locator('#editable')).to_have_text('Edited while zoomed')
        expect(zoom).to_have_text('90%')
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-markup-view.png'))
        print("PASS: editing and clean saves still work; view settings stay with each open document", flush=True)

        page.set_viewport_size({"width":600, "height":900})
        expect(controls).to_be_visible()
        assert controls.bounding_box()['x'] + controls.bounding_box()['width'] <= 600
        page.evaluate("path => openFile(path)", str(svg))
        expect(frame.locator('text')).to_have_text('Large SVG')
        controls.get_by_role("button", name="Fit width", exact=True).click()
        page.wait_for_function("activeTab().markupZoom < .3")
        assert window_eval("innerWidth") >= 2400
        page.evaluate("path => switchTab(path)", notebook)
        expect(controls).not_to_be_visible()
        assert not errors, errors
        print("PASS: compact controls support SVG and stay out of notebook views", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
