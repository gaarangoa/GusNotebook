"""Notebook math rendering, literal code, safe errors, and source round trips."""

import os
from pathlib import Path

import nbformat
from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    notebook = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work' / 'math.ipynb'
    sources = [
        r'Inline **math**: $x_i + y_j = \frac{1}{2}$ and \(\sqrt{x}\).',
        r'''Display equation:
$$
\begin{aligned}
a_i &= b_i + c_i \\
x &= \frac{1}{2}
\end{aligned}
$$
After the equation.''',
        r'\[\sum_{i=1}^{n} i = \frac{n(n+1)}{2}\]',
        r'''Literal `$x_i$` and `\(x\)`; escaped \$5 and \$10; prices $5 and $10.

```latex
$$\frac{a}{b}$$
```

Unclosed $x and \(y.''',
        r'Bad $\frac{1}{$ followed by valid $x^2$.',
        r'''<img src="missing" onerror="window.mathXss = true">
<script>window.mathXss = true</script>

$\href{javascript:alert(1)}{click}$
$\htmlStyle{position:fixed}{x}$''',
        r'''| Quantity | Formula |
| --- | --- |
| Energy | $E=mc^2$ |

- Inline $a_i$ in a list
- Display:
  $$x^2$$
''',
    ]
    nbformat.write(nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_markdown_cell(source) for source in sources
    ]), notebook)

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors, asset_errors = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: asset_errors.append(response.url)
                if '/static/vendor/' in response.url and response.status >= 400 else None)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('path => openFile(path)', str(notebook))
        rendered = page.locator('.cell .md-rendered')
        expect(rendered).to_have_count(len(sources))
        expect(rendered.nth(0).locator('.katex')).to_have_count(2)
        expect(rendered.nth(0).locator('strong')).to_have_text('math')
        expect(rendered.nth(0).locator('annotation').first).to_have_text(r'x_i + y_j = \frac{1}{2}')
        expect(rendered.nth(1).locator('.katex-display')).to_have_count(1)
        expect(rendered.nth(1).locator('.katex-error')).to_have_count(0)
        expect(rendered.nth(1)).to_contain_text('After the equation.')
        expect(rendered.nth(2).locator('.katex-display')).to_have_count(1)
        expect(rendered.nth(3).locator('.katex')).to_have_count(0)
        expect(rendered.nth(3).locator('pre code')).to_have_text(r'$$\frac{a}{b}$$' + '\n')
        expect(rendered.nth(3)).to_contain_text('prices $5 and $10')
        expect(rendered.nth(4).locator('.katex-error')).to_have_count(1)
        expect(rendered.nth(4).locator('.katex')).to_have_count(1)
        expect(rendered.nth(5).locator('script, [onerror], a, [style*="fixed"]')).to_have_count(0)
        assert page.evaluate('window.mathXss') is None
        expect(rendered.nth(6).locator('td .katex')).to_have_count(1)
        expect(rendered.nth(6).locator('li .katex')).to_have_count(2)
        assert page.evaluate('cells.map(cell => cell.source)') == sources
        page.evaluate('document.fonts.ready')
        assert page.evaluate('document.fonts.check("16px KaTeX_Main")')
        assert not asset_errors, asset_errors
        print('PASS: inline/display math, multiline TeX, literal code, and safe malformed input', flush=True)

        for theme in ('light', 'dark'):
            page.evaluate('theme => AppAppearance.update({theme, fontSize: 14})', theme)
            expect(rendered.nth(0).locator('.katex').first).to_be_visible()
            assert rendered.nth(0).evaluate('el => getComputedStyle(el).color === getComputedStyle(el.querySelector(".katex")).color')

        cell_id = page.evaluate('cells[0].id')
        rendered.nth(0).dblclick()
        editor = page.locator(f'#ed-{cell_id} .cm-content')
        expect(editor).to_be_visible()
        expect(editor).to_have_text(sources[0])
        edited = r'Updated $\alpha^2 + \beta^2 = 1$.'
        editor.fill(edited)
        editor.press('Shift+Enter')
        expect(rendered.nth(0).locator('annotation')).to_have_text(r'\alpha^2 + \beta^2 = 1')
        assert nbformat.read(notebook, as_version=4).cells[0].source == edited
        page.reload(wait_until='domcontentloaded')
        expect(page.locator('.cell .md-rendered').first.locator('annotation')).to_have_text(r'\alpha^2 + \beta^2 = 1')
        assert not errors, errors
        browser.close()
        print('PASS: themes and edit/render/save/reload preserve the original LaTeX source', flush=True)


if __name__ == '__main__':
    main()
