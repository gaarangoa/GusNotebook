"""Markdown styles, live settings preview, cancellation, and persistence."""

import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    document = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work' / 'style review.md'
    original = r'''# Experiment notes

Compare the two cohorts and keep the observations alongside the calculation.

## Results

> Record the inputs so the experiment can be reproduced.

| Cohort | Mean |
| --- | --- |
| Control | 12.4 |
| Treatment | 15.8 |

The estimate is $\hat{\mu} = \frac{1}{n}\sum_i x_i$.

$$\sigma^2 = \frac{1}{n}\sum_i (x_i - \mu)^2$$

```python
values = [12.4, 15.8]
print(sum(values) / len(values))
```
'''
    document.write_text(original)
    screenshots = Path(tempfile.gettempdir()) / 'gusnb-markdown-styles'
    screenshots.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('path => openFile(path)', str(document))
        preview = page.locator('#markdown-preview')
        root = page.locator('html')
        expect(root).to_have_attribute('data-markdown-style', 'compact')
        expect(preview).to_have_css('font-size', '12px')
        page.click('#markdown-source-button')
        draft = original + '\nUnsaved observation.\n'
        page.locator('#text-editor').fill(draft)
        page.click('#markdown-preview-button')
        # Styles apply through CSS and retain the existing rendered document.
        page.evaluate('window.styleReviewContent = document.querySelector(".markdown-content")')
        for style, size in [('reading', 16), ('paper', 18), ('compact', 12)]:
            page.click('#settings-button')
            expect(page.locator('#settings-back')).to_be_visible()
            expect(page.locator('#set-markdown-style')).to_have_value('compact')
            page.get_by_label('Markdown preview style', exact=True).select_option(style)
            expect(root).to_have_attribute('data-markdown-style', style)
            expect(preview).to_have_css('font-size', f'{size}px')
            assert page.evaluate('document.querySelector(".markdown-content") === styleReviewContent')
            assert page.evaluate('activeTab().text') == draft
            assert document.read_text() == original
            page.keyboard.press('Escape')
            expect(root).to_have_attribute('data-markdown-style', 'compact')
            expect(page.locator('#settings-button')).to_be_focused()
        print('PASS: Compact defaults correctly; live previews and Cancel preserve unsaved source and DOM', flush=True)

        for style, size in [('reading', 16), ('paper', 18), ('compact', 12)]:
            page.click('#settings-button')
            page.get_by_label('Markdown preview style', exact=True).select_option(style)
            page.click('#settings-save')
            expect(page.locator('#settings-back')).not_to_be_visible()
            # Saving settings must not save the document draft.
            assert document.read_text() == original
            assert page.evaluate('JSON.parse(localStorage.getItem("gusnotebook.appearance")).markdownStyle') == style
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('booted')
            expect(root).to_have_attribute('data-markdown-style', style)
            expect(preview).to_have_css('font-size', f'{size}px')
            page.click('#settings-button')
            expect(page.locator('#set-markdown-style')).to_have_value(style)
            page.keyboard.press('Escape')
            font = preview.evaluate('el => getComputedStyle(el).fontFamily')
            assert ('Georgia' in font) == (style == 'paper')
            content = preview.locator('.markdown-content').bounding_box()
            pane = preview.bounding_box()
            if style == 'compact':
                assert content['width'] > pane['width'] - 60
            else:
                assert content['width'] < pane['width'] - 80
                assert abs(content['x'] + content['width'] / 2 - pane['x'] - pane['width'] / 2) < 2
            for theme in ('light', 'dark'):
                page.evaluate('theme => AppAppearance.update({theme})', theme)
                expect(preview.locator('.katex')).to_have_count(2)
                expect(preview.locator('.katex-display')).to_have_count(1)
                expect(preview.locator('.katex-error')).to_have_count(0)
                expect(preview.locator('.hljs-number').first).to_have_text('12.4')
                page.screenshot(path=str(screenshots / f'{style}-{theme}.png'))
            for width in (768, 390, 320):
                page.set_viewport_size({'width': width, 'height': 900})
                page.wait_for_function('document.querySelector("#markdown-preview").clientWidth >= innerWidth - 44')
                assert preview.evaluate('el => el.scrollWidth <= el.clientWidth')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width': 1920, 'height': 1080})
        print('PASS: all styles save/reload, retain math and highlighting, and fit light/dark mobile layouts', flush=True)

        # Older browsers' saved preferences adopt Compact without losing other fields.
        page.evaluate('''() => localStorage.setItem('gusnotebook.appearance', JSON.stringify({
          theme: 'light', density: 'compact', fontSize: 14
        }))''')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('booted')
        assert page.evaluate('AppAppearance.get()') == {
            'theme': 'light', 'density': 'compact', 'fontSize': 14, 'markdownStyle': 'compact'}
        expect(preview).to_have_css('font-size', '14px')
        assert not errors, errors
        browser.close()
        print('PASS: existing appearance preferences migrate to the Compact default', flush=True)


if __name__ == '__main__':
    main()
