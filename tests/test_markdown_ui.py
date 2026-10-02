"""Rendered Markdown, source editing, local navigation, and safe external reloads."""

import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    work = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]).resolve() / "work"
    docs = work / "docs # α"
    (docs / "assets").mkdir(parents=True)
    (docs / "assets" / "plot #1.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="20"><rect width="30" height="20" fill="teal"/></svg>')
    guide = docs / "guide notes.markdown"
    guide.write_text("# Guide\n\n## Results\n\nLinked document.\n")
    document = docs / "README.md"
    original = '''# Research notes

**Bold** and *italic* with `inline code`.

## Methods

- First item
- Second item
- [x] Reviewed

| Sample | Value |
| --- | --- |
| A | 42 |

```python
print("hello")
```

![Plot](assets/plot%20%231.svg)

[Guide](guide%20notes.markdown#results) · [Methods](#methods) · [External](https://example.com)

<script>window.markdownXss = true</script>
<style>body {display:none}</style>
<img src="missing.png" onerror="window.markdownXss = true">
<div id="toolbar" class="app-header" style="position:fixed;inset:0">Plain HTML text</div>

[Unsafe](javascript:alert(1))
'''
    document.write_text(original)
    math_source = r'''# Equations

Inline **math**: $x_i + y_j = \frac{1}{2}$ and \(\sqrt{x}\).

$$
\begin{aligned}
a_i &= b_i + c_i \\
x &= \frac{1}{2}
\end{aligned}
$$

\[\sum_{i=1}^{n} i = \frac{n(n+1)}{2}\]

| Quantity | Formula |
| --- | --- |
| Energy | $E=mc^2$ |

- Inline $a_i$ in a list

Literal `$x_i$` and `\(x\)`; escaped \$5 and \$10; prices $5 and $10.

```latex
$$\frac{a}{b}$$
```

Unclosed $x and \(y.

Bad $\frac{1}{$ followed by valid $x^2$.

<script>window.markdownMathXss = true</script>
<div id="toolbar" class="katex" style="position:fixed;inset:0">Raw HTML</div>
<img src="missing.png" onerror="window.markdownMathXss = true">

$\href{javascript:alert(1)}{click}$
$\htmlStyle{position:fixed}{x}$
'''
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        authenticate_browser(page.context, url)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("booted && tabs.length")
        notebook = page.evaluate("active")
        page.evaluate("path => openFile(path)", str(document))
        preview = page.locator("#markdown-preview")
        source = page.locator("#text-editor")
        expect(preview).to_be_visible()
        expect(source).not_to_be_visible()
        expect(preview.locator("h1")).to_have_text("Research notes")
        expect(preview.locator("strong")).to_have_text("Bold")
        expect(preview.locator("table")).to_contain_text("42")
        expect(preview.locator("pre code")).to_have_text('print("hello")\n')
        expect(preview.locator('input[type="checkbox"]')).to_be_checked()
        page.wait_for_function("document.querySelector('#markdown-preview img[alt=Plot]').naturalWidth === 30")
        assert not preview.locator("script, style, [style], [id], [class], [onerror]").count()
        assert page.evaluate("window.markdownXss") is None
        assert page.locator("#toolbar").count() == 1
        assert preview.get_by_text("Unsafe", exact=True).get_attribute("href") is None
        expect(preview.get_by_role("link", name="External")).to_have_attribute("rel", "noopener noreferrer")
        assert document.read_text() == original
        print("PASS: Markdown renders by default, resolves local images, and strips active HTML", flush=True)

        for extension in ("md", "markdown"):
            math_document = docs / f"equations.{extension}"
            math_document.write_text(math_source)
            page.evaluate("path => openFile(path)", str(math_document))
            expect(preview.locator(".katex")).to_have_count(9)
            expect(preview.locator(".katex-display")).to_have_count(2)
            expect(preview.locator(".katex-error")).to_have_count(1)
            expect(preview.locator("annotation").first).to_have_text(r"x_i + y_j = \frac{1}{2}")
            expect(preview.locator("td .katex")).to_have_count(1)
            expect(preview.locator("li .katex")).to_have_count(1)
            expect(preview.locator("pre code")).to_have_text(r"$$\frac{a}{b}$$" + "\n")
            expect(preview.locator("code .katex")).to_have_count(0)
            expect(preview).to_contain_text("prices $5 and $10")
            expect(preview.locator("script, [onerror], [id], [style*='fixed'], a")).to_have_count(0)
            expect(preview.get_by_text("Raw HTML", exact=True)).not_to_have_attribute("class", "katex")
            assert page.evaluate("window.markdownMathXss") is None
            page.evaluate("document.fonts.ready")
            assert page.evaluate('document.fonts.check("16px KaTeX_Main")')
            for theme in ("light", "dark"):
                page.evaluate("theme => AppAppearance.update({theme, fontSize: 14})", theme)
                expect(preview.locator(".katex").first).to_be_visible()
                assert preview.evaluate('el => getComputedStyle(el).color === getComputedStyle(el.querySelector(".katex")).color')
            page.click("#markdown-source-button")
            expect(source).to_have_value(math_source)
            edited_math = math_source.replace(r"\sqrt{x}", r"\sqrt{y}")
            source.fill(edited_math)
            page.click("#markdown-preview-button")
            expect(preview.locator("annotation").nth(1)).to_have_text(r"\sqrt{y}")
            assert math_document.read_text() == math_source
            preview.focus()
            page.keyboard.press("ControlOrMeta+s")
            expect(page.locator("#text-status")).to_have_text("saved")
            assert math_document.read_text() == edited_math
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("booted")
            expect(preview.locator("annotation").nth(1)).to_have_text(r"\sqrt{y}")
        page.evaluate("path => switchTab(path)", str(document))
        print("PASS: .md/.markdown render math safely and preserve TeX through edit/save/reload", flush=True)

        for theme in ("light", "dark"):
            page.evaluate("theme => AppAppearance.update({theme, fontSize: 10})", theme)
            expect(preview).to_have_css("font-size", "10px")
            expect(preview.locator("h1")).to_have_css("font-size", "20px")
            expect(preview.locator("h2")).to_have_css("font-size", "15px")
            colors = preview.evaluate("el => [getComputedStyle(el).color, getComputedStyle(el).backgroundColor]")
            assert colors[0] != colors[1]
            page.screenshot(path=str(Path(tempfile.gettempdir()) / f"gusnb-markdown-{theme}.png"))
        preview.get_by_role("link", name="Methods", exact=True).click()
        assert page.evaluate("active") == str(document)
        preview.get_by_role("link", name="Guide", exact=True).click()
        expect(preview.locator("h1")).to_have_text("Guide")
        assert page.evaluate("active") == str(guide)
        page.evaluate("path => switchTab(path)", str(document))
        print("PASS: heading hierarchy follows theme/font settings and local Markdown links open tabs", flush=True)

        page.click("#markdown-source-button")
        expect(source).to_be_visible()
        expect(source).to_have_value(original)
        edited = original.replace("Research notes", "Edited notes")
        source.fill(edited)
        page.click("#markdown-preview-button")
        expect(preview.locator("h1")).to_have_text("Edited notes")
        assert document.read_text() == original
        page.evaluate("path => switchTab(path)", notebook)
        expect(page.locator("#markdown-view")).not_to_be_visible()
        with page.expect_response(lambda response: "/api/sessions/" in response.url
                                  and response.request.method == "POST"
                                  and response.request.post_data_json.get("active") == str(document)
                                  and response.ok):
            page.evaluate("path => switchTab(path)", str(document))
        expect(preview.locator("h1")).to_have_text("Edited notes")
        preview.focus()
        page.keyboard.press("ControlOrMeta+s")
        expect(page.locator("#text-status")).to_have_text("saved")
        assert document.read_text() == edited
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("booted")
        expect(preview.locator("h1")).to_have_text("Edited notes")
        print("PASS: source/preview switches preserve drafts; keyboard save and reopening retain Markdown", flush=True)

        document.write_text("# External change\n\nChanged by an agent.\n")
        expect(preview.locator("h1")).to_have_text("External change", timeout=10000)
        page.click("#markdown-source-button")
        source.fill("# My unsaved draft\n")
        page.click("#markdown-preview-button")
        document.write_text("# Another disk change\n")
        expect(page.locator("#text-status")).to_contain_text("changed on disk", timeout=10000)
        expect(preview.locator("h1")).to_have_text("My unsaved draft")
        page.click("#text-reload")
        expect(page.locator("#ask-back")).to_be_visible()
        page.keyboard.press("Escape")
        expect(preview.locator("h1")).to_have_text("My unsaved draft")
        page.click("#text-reload")
        page.click("#ask-ok")
        expect(preview.locator("h1")).to_have_text("Another disk change")
        for width in (768, 390):
            page.set_viewport_size({"width": width, "height": 900})
            expect(page.locator("#markdown-source-button")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        browser.close()
        print("PASS: external edits refresh clean previews and require confirmation before discarding drafts", flush=True)


if __name__ == "__main__":
    main()
