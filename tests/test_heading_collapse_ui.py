"""Nested heading collapse preserves live editors, outputs and saved view state."""

import os
from pathlib import Path
import tempfile

import nbformat
from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    notebook = work / 'sections.ipynb'
    sources = [
        '# Results', '\n'.join(f'value_{i} = {i}' for i in range(30)),
        '## Detail', 'detail = 1', '## Next detail', 'other = 2',
        '# Discussion', 'discussion = 3',
        '```markdown\n# Literal heading\n```',
        'Summary\n=======', 'summary = 4', 'Follow-up\n-------', 'follow_up = 5',
        '## Empty', '## Last', 'last = 6',
    ]
    markdown_indices = {0, 2, 4, 6, 8, 9, 11, 13, 14}
    document = nbformat.v4.new_notebook(cells=[
        (nbformat.v4.new_markdown_cell if i in markdown_indices else nbformat.v4.new_code_cell)(source)
        for i, source in enumerate(sources)])
    document.cells[1].outputs = [nbformat.v4.new_output('stream', name='stdout', text='Existing output\n')]
    nbformat.write(document, notebook)
    original = notebook.read_bytes()
    ids = [cell.id for cell in document.cells]
    screenshots = Path(tempfile.gettempdir()) / 'gusnb-notebook-sections'
    screenshots.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        other = page.evaluate('active')
        page.evaluate('path => openFile(path)', str(notebook))
        page.wait_for_selector(f'#ed-{ids[1]} .cm-editor')

        def cell(index):
            return page.locator(f'.cell[data-id="{ids[index]}"]')

        def toggle(index):
            return cell(index).locator('.hd-toggle')

        page.evaluate('id => {window.retainedCode = document.querySelector(`#ed-${id} .cm-editor`);}', ids[1])
        expect(toggle(0)).to_have_attribute('aria-expanded', 'true')
        expect(cell(0).locator('.heading-summary')).not_to_be_visible()
        expect(cell(8).locator('.hd-toggle')).to_have_count(0)
        expect(cell(13).locator('.hd-toggle')).to_have_count(0)
        toggle(2).click()
        expect(cell(3)).not_to_be_visible()
        expect(cell(4)).to_be_visible()
        expect(cell(2).locator('.heading-summary')).to_have_text('1 cell hidden')
        expect(toggle(2)).to_have_attribute('aria-expanded', 'false')
        toggle(0).click()
        for index in range(1, 6):
            expect(cell(index)).not_to_be_visible()
        expect(cell(6)).to_be_visible()
        expect(cell(0).locator('.heading-summary')).to_have_text('5 cells hidden')
        expect(toggle(0)).to_have_attribute('aria-expanded', 'false')
        cell(0).locator('.heading-summary').click()
        expect(cell(1)).to_be_visible()
        expect(cell(2)).to_be_visible()
        expect(cell(3)).not_to_be_visible()
        expect(cell(4)).to_be_visible()
        assert page.evaluate('id => retainedCode === document.querySelector(`#ed-${id} .cm-editor`)', ids[1])
        expect(cell(1).locator('.output-area')).to_contain_text('Existing output')
        toggle(2).focus()
        page.keyboard.press('Enter')
        expect(cell(3)).to_be_visible()
        expect(toggle(2)).to_have_attribute('aria-expanded', 'true')
        print('PASS: nested heading boundaries, hidden-cell summaries, keyboard controls, editor identity and outputs', flush=True)

        toggle(9).click()
        for index in range(10, 16):
            expect(cell(index)).not_to_be_visible()
        expect(cell(6)).to_be_visible()
        cell(9).locator('.heading-summary').click()
        toggle(11).click()
        expect(cell(12)).not_to_be_visible()
        expect(cell(13)).to_be_visible()
        expect(cell(14)).to_be_visible()
        page.evaluate('path => switchTab(path)', other)
        page.evaluate('path => switchTab(path)', str(notebook))
        expect(cell(12)).not_to_be_visible()
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('booted')
        expect(cell(12)).not_to_be_visible()
        expect(toggle(11)).to_have_attribute('aria-expanded', 'false')
        assert notebook.read_bytes() == original
        assert page.evaluate('cells.map(c => c.source)') == sources
        print('PASS: Setext headings, fenced-heading exclusion, tab switching, reload and unchanged notebook data', flush=True)

        # Exercise the outline's folded/expanded states alongside the controls.
        for theme in ('light', 'dark'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            fold = cell(1).locator('.fold')
            expect(fold).to_have_class('fold folded')
            height = fold.bounding_box()['height']
            cell(1).locator('.fold-veil span').click()
            expect(fold).to_have_class('fold')
            assert fold.bounding_box()['height'] > height
            cell(1).locator('.gutter-out button').click()
            expect(fold).to_have_class('fold folded')
            assert abs(fold.bounding_box()['height'] - height) < 1
            page.screenshot(path=str(screenshots / f'{theme}.png'))
        assert not errors, errors
        print('PASS: folded code still expands/refolds at the same height in light and dark themes; no browser errors', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
