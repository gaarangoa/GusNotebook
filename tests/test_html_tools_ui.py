"""HTML cards, text formatting, image paste, undo, and portable saves."""

import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    work = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]).resolve() / "work"
    report = work / "report.html"
    original = '''<!doctype html><html><head><style>
body {font:16px/1.6 system-ui; margin:36px; color:#263244; background:white}
main {max-width:760px;margin:auto} h1 {font-size:28px}
</style></head><body><main>
<h1>Research report</h1><p id="intro">First <em>important</em> result and <a href="#data">reference</a>.</p>
<p id="after">Another paragraph.</p><div id="chart"></div>
<button id="interactive">Chart action</button>
<script>
const chart = document.getElementById('chart');
const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
svg.innerHTML = '<text x="5" y="20">Generated chart label</text>';
chart.appendChild(svg);
document.getElementById('interactive').addEventListener('click', function () {
  this.dataset.clicks = String(Number(this.dataset.clicks || 0) + 1);
});
</script></main></body></html>'''
    report.write_text(original)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width":1440, "height":1000},
                                permissions=['clipboard-read', 'clipboard-write'])
        authenticate_browser(page.context, url)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("booted")
        notebook = page.evaluate("active")
        page.evaluate("path => openFile(path)", str(report))
        frame = page.frame_locator("#html-preview-frame")
        tools = frame.locator('[data-gusnotebook-runtime="authoring"]')
        expect(tools).to_be_attached()
        formatting = tools.get_by_role("toolbar", name="Text formatting")

        def select(selector, collapse=False):
            frame.locator(selector).evaluate("""(el, collapse) => {
              window.focus(); document.body.focus({preventScroll:true});
              const range = document.createRange(); range.selectNodeContents(el);
              if (collapse) range.collapse(false);
              const selection = document.getSelection(); selection.removeAllRanges(); selection.addRange(range);
              el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
            }""", collapse)

        def number(label, value):
            field = tools.get_by_role("spinbutton", name=label, exact=True)
            field.fill(str(value)); field.press("Enter")

        def selects_html(response, html):
            if not response.url.endswith('/api/markup-focus') or not response.ok:
                return False
            selection = response.request.post_data_json.get('selection')
            return bool(selection and selection['document'][selection['start']:selection['end']] == html)

        frame.locator('#after').evaluate("""el => {
          const range=document.createRange();range.setStart(el.firstChild,0);range.setEnd(el.firstChild,7);
          const selection=document.getSelection();selection.removeAllRanges();selection.addRange(range);
          el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
        }""")
        number("Font size", 18)
        expect(frame.locator('#after span')).to_have_text('Another')
        formatting.get_by_role("button", name="Undo", exact=True).click()
        assert frame.locator('#after').inner_html() == 'Another paragraph.'

        select("#intro")
        expect(formatting).to_be_visible()
        number("Font size", 22)
        expect(frame.locator("#intro em span")).to_have_css("font-size", "22px")
        expect(frame.locator("#intro a")).to_have_attribute("href", "#data")
        expect(frame.locator("#after")).to_have_css("font-size", "16px")
        formatting.get_by_role("button", name="Bold", exact=True).click()
        expect(frame.locator("#intro em span")).to_have_css("font-weight", "700")
        formatting.get_by_role("button", name="Italic", exact=True).click()
        expect(frame.locator("#intro a span")).to_have_css("font-style", "italic")
        tools.get_by_label("Text color", exact=True).evaluate("el => {el.value='#123456';el.dispatchEvent(new Event('change', {bubbles:true}));}")
        expect(frame.locator("#intro a span")).to_have_css("color", "rgb(18, 52, 86)")
        # Agent context generation must not split live text nodes or damage undo.
        page.wait_for_function("reportedMarkupPath === active")
        formatting.get_by_role("button", name="Undo", exact=True).click()
        expect(frame.locator("#intro a span")).not_to_have_css("color", "rgb(18, 52, 86)")
        page.keyboard.press("ControlOrMeta+Shift+z")
        expect(frame.locator("#intro a span")).to_have_css("color", "rgb(18, 52, 86)")
        expected_html = frame.locator('#intro').inner_html()
        with page.expect_response(lambda response: selects_html(response, expected_html)) as selection_response:
            select('#intro')
        selection_data = selection_response.value.request.post_data_json['selection']
        selected_html = selection_data['document'][selection_data['start']:selection_data['end']]
        assert selected_html == frame.locator('#intro').inner_html(), (selected_html, frame.locator('#intro').inner_html())
        print("PASS: formatting preserves nested markup, links, agent selection, and undo/redo", flush=True)

        select("#intro", collapse=True)
        page.get_by_role("button", name="Insert card", exact=True).click()
        card = frame.locator('[data-gusnb-card="1"]')
        expect(card).to_have_count(1)
        assert card.evaluate("el => el.previousElementSibling.id") == "intro"
        select('[data-gusnb-card] h3')
        page.keyboard.insert_text("Study summary")
        expect(card.locator("h3")).to_have_text("Study summary")
        page.keyboard.press("ControlOrMeta+z")
        expect(card.locator("h3")).to_have_text("Card title")
        page.keyboard.press("ControlOrMeta+Shift+z")
        expect(card.locator("h3")).to_have_text("Study summary")
        # Click padding to customize the card without selecting its text.
        card.click(position={"x":4,"y":4})
        expect(tools.get_by_role("group", name="Card settings")).to_be_visible()
        number("Card width percent", 75)
        number("Card padding", 28)
        number("Card border width", 2)
        tools.get_by_label("Card background", exact=True).evaluate("el => {el.value='#eaf3ff';el.dispatchEvent(new Event('change', {bubbles:true}));}")
        expect(card).to_have_css("padding", "28px")
        expect(card).to_have_css("background-color", "rgb(234, 243, 255)")
        tools.get_by_role("button", name="Delete card", exact=True).click()
        expect(card).to_have_count(0)
        frame.locator("#after").click()
        page.keyboard.press("ControlOrMeta+z")
        expect(card).to_have_count(1)
        expect(card.locator("h3")).to_have_text("Study summary")
        print("PASS: cards insert between blocks; typing, styling, deletion, and undo preserve content", flush=True)

        # Both clipboard screenshots and image files use this HTML-only route.
        for mime in ("image/png", "image/jpeg"):
            select("#after", collapse=True)
            if mime == "image/png":
                page.evaluate("""async () => {
                  const canvas=document.createElement('canvas');canvas.width=1200;canvas.height=600;
                  const ctx=canvas.getContext('2d');ctx.fillStyle='teal';ctx.fillRect(0,0,1200,600);
                  const blob=await new Promise(resolve => canvas.toBlob(resolve,'image/png'));
                  await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
                }""")
                page.keyboard.press("ControlOrMeta+v")
            else:
                frame.locator("#after").evaluate("""async (el, type) => {
              const canvas = document.createElement('canvas'); canvas.width=1200;canvas.height=600;
              const ctx=canvas.getContext('2d');ctx.fillStyle='teal';ctx.fillRect(0,0,1200,600);
              const blob=await new Promise(resolve => canvas.toBlob(resolve,type));
              const transfer=new DataTransfer(); transfer.items.add(new File([blob], 'plot.'+(type==='image/png'?'png':'jpg'), {type}));
              el.dispatchEvent(new ClipboardEvent('paste', {bubbles:true,cancelable:true,clipboardData:transfer}));
                }""", mime)
            images = frame.locator('img[src^="pasted-image-"]')
            expect(images).to_have_count(1 if mime == "image/png" else 2)
            pasted = images.last
            expect(pasted).to_have_css("width", "240px")
            expect(pasted).to_have_css("height", "120px")
            tools.get_by_role("button", name="Make image larger").click()
            expect(pasted).to_have_css("width", "300px")
            tools.get_by_role("button", name="Make image smaller").click()
            expect(pasted).to_have_css("width", "240px")
            number("Image width", 360)
            expect(pasted).to_have_css("height", "180px")
        assert len(list(work.glob("pasted-image-*"))) == 2
        tools.get_by_role("toolbar", name="Image size").get_by_role("button", name="Undo", exact=True).click()
        expect(images.last).to_have_css("width", "240px")
        select("#after", collapse=True)
        page.evaluate("""async () => {
          const canvas=document.createElement('canvas');canvas.width=1200;canvas.height=600;
          const html='<img alt="HTML clipboard image" width="1200" height="600" src="'+canvas.toDataURL()+'">';
          await navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([html],{type:'text/html'})})]);
        }""")
        page.keyboard.press("ControlOrMeta+v")
        expect(frame.get_by_alt_text('HTML clipboard image')).to_have_css('width', '240px')
        page.keyboard.press("ControlOrMeta+z")
        expect(frame.get_by_alt_text('HTML clipboard image')).to_have_count(0)
        print("PASS: PNG/JPEG pastes start small and resize proportionally with original files retained", flush=True)

        frame.locator("#interactive").click()
        expect(frame.locator("#interactive")).to_have_attribute("data-clicks", "1")
        select("#chart svg text")
        expect(formatting).not_to_be_visible()
        page.click("#text-save")
        page.wait_for_function("!activeTab().dirty && !activeTab().saveInFlight")
        saved = report.read_text()
        assert 'data-gusnotebook-runtime' not in saved
        assert 'contenteditable="true"' not in saved
        assert '<svg' not in saved  # generated SVG remains generated, not duplicated
        assert 'data-gusnb-card="1"' in saved and 'pasted-image-' in saved
        assert 'data:image/' not in saved
        page.click("#text-reload")
        expect(frame.locator('[data-gusnb-card] h3')).to_have_text("Study summary")
        expect(frame.locator('img[src^="pasted-image-"]')).to_have_count(2)
        expect(frame.locator('#chart svg')).to_have_count(1)
        frame.locator("#interactive").click()
        expect(frame.locator("#interactive")).to_have_attribute("data-clicks", "2")
        for theme in ("dark", "light"):
            page.evaluate("theme => AppAppearance.update({theme})", theme)
            select("#intro")
            expect(formatting).to_be_visible()
            expect(tools).to_have_attribute("data-dark", "") if theme == "dark" else expect(tools).not_to_have_attribute("data-dark", "")
            page.screenshot(path=str(Path(tempfile.gettempdir()) / f"gusnb-html-tools-{theme}.png"))
        page.set_viewport_size({"width":600, "height":900})
        page.locator('#html-preview-frame').element_handle().content_frame().wait_for_function("""() => {
          const panel = document.querySelector('[data-gusnotebook-runtime="authoring"]').shadowRoot.getElementById('text');
          return panel.getBoundingClientRect().right <= innerWidth;
        }""")
        diagram = work / 'diagram.svg'
        diagram.write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><text x="10" y="20">SVG title</text></svg>')
        page.evaluate('path => openFile(path)', str(diagram))
        expect(frame.locator('text')).to_have_text('SVG title')
        expect(tools).not_to_be_attached()
        expect(page.locator('#html-insert-card')).not_to_be_visible()
        with page.expect_response(lambda response: selects_html(response, 'SVG title')) as svg_selection:
            select('text')
        selection_data = svg_selection.value.request.post_data_json['selection']
        assert selection_data['document'].startswith('<?xml version="1.0"?>')
        assert selection_data['document'][selection_data['start']:selection_data['end']] == 'SVG title'
        page.evaluate("path => switchTab(path)", notebook)
        expect(page.locator("#html-insert-card")).not_to_be_visible()
        cell = page.evaluate("addCell('code')")
        paste_text = "# ordinary notebook paste\nprint('still works')"
        page.evaluate("text => navigator.clipboard.writeText(text)", paste_text)
        page.locator(f"#ed-{cell['id']} .cm-content").click()
        page.keyboard.press("ControlOrMeta+v")
        page.wait_for_function("args => cmViews.get(args.id).state.doc.toString() === args.text",
                               arg={"id":cell['id'], "text":paste_text})
        page.keyboard.press("ControlOrMeta+z")
        page.wait_for_function("id => cmViews.get(id).state.doc.toString() === ''", arg=cell['id'])
        assert len(list(work.glob("pasted-image-*"))) == 2
        assert not errors, errors
        print("PASS: clean HTML, charts, images, themes, compact controls, and notebook clipboard behavior", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
