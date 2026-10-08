"""Structured JSON viewing, exact source edits, bounded rendering and fallback."""

import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    document = work / 'analysis # α.JSON'
    original = '''{
  "study": "Response analysis",
  "samples": [{"id":9007199254740993,"response":0.123400e+10}],
  "parameters": {"threshold":0.05,"approved":true,"notes":null},
  "duplicate":1,"duplicate":2,
  "html":"<img src=x onerror=window.jsonXss=true>",
  "empty":{}
}\n'''
    document.write_text(original)
    csv = work / 'data.csv'
    csv.write_text('Name,Value\nA,1\n')
    large_list = work / 'list.json'
    large_list.write_text(json.dumps(list(range(1500))))
    invalid = work / 'invalid.json'
    invalid.write_text('{bad')
    deep = work / 'deep.json'
    deep_source = '[' * 65 + '0' + ']' * 65
    deep.write_text(deep_source)
    dense = work / 'dense.json'
    dense.write_text(json.dumps([0] * 6000))
    for suffix in ('jsonl', 'ndjson'):
        (work / ('records.' + suffix)).write_text('{"value":1}\n{"value":2}\n')

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('AppAppearance.update({fontSize: 10})')
        notebook = page.evaluate('active')
        preview = page.locator('#json-preview')
        editor = page.locator('#text-editor')

        def open_file(path):
            page.evaluate('path => openFile(path)', str(path))

        open_file(document)
        expect(preview).to_be_visible()
        expect(page.locator('#table-view')).not_to_be_visible()
        expect(page.locator('#table-preview')).not_to_be_visible()
        expect(editor).not_to_be_visible()
        assert page.evaluate("getComputedStyle(document.getElementById('json-preview')).fontSize") == '10px'
        samples = preview.locator('summary').filter(has_text='"samples"')
        samples.focus()
        samples.press('Enter')
        sample = samples.locator('..').locator('.json-children > details > summary')
        sample.click()
        expect(preview.locator('.json-number').filter(has_text='9007199254740993')).to_be_visible()
        expect(preview.locator('.json-number').filter(has_text='0.123400e+10')).to_be_visible()
        expect(preview.locator('.json-key').filter(has_text='"duplicate"')).to_have_count(2)
        expect(preview.locator('img')).to_have_count(0)
        assert not page.evaluate('!!window.jsonXss')
        samples.click()
        expect(preview.locator('.json-number').filter(has_text='9007199254740993')).not_to_be_visible()
        samples.click()
        open_file(csv)
        expect(page.locator('#table-preview')).to_be_visible()
        expect(preview).not_to_be_visible()
        open_file(document)
        expect(preview.locator('.json-number').filter(has_text='9007199254740993')).to_be_visible()
        page.evaluate('path => switchTab(path)', notebook)
        expect(preview).not_to_be_visible()
        open_file(document)
        page.click('#json-source-button')
        expect(editor).to_have_value(original)
        draft = original.replace('Response analysis', 'Updated analysis')
        editor.fill(draft)
        page.click('#json-preview-button')
        expect(preview).to_contain_text('Updated analysis')
        assert document.read_text() == original
        page.click('#text-save')
        page.wait_for_function('!activeTab().dirty && !activeTab().saveInFlight')
        assert document.read_text() == draft
        print('PASS: collapsible JSON, keyboard controls, exact numbers/keys, safe text, tab state and source saves', flush=True)

        open_file(large_list)
        expect(preview.locator('.json-number')).to_have_count(100)
        page.click('#json-preview .json-more')
        expect(preview.locator('.json-number')).to_have_count(200)
        preview.evaluate('element => element.scrollTop = 500')
        scroll = preview.evaluate('element => element.scrollTop')
        open_file(csv)
        open_file(large_list)
        assert preview.evaluate('element => element.scrollTop') == scroll
        expect(preview.locator('.json-number')).to_have_count(200)
        open_file(invalid)
        expect(editor).to_be_visible()
        expect(page.locator('#json-notice')).to_contain_text('Invalid JSON')
        editor.fill('{"fixed":42}')
        page.click('#json-preview-button')
        expect(preview).to_contain_text('42')
        expect(page.locator('#json-notice')).not_to_be_visible()
        assert invalid.read_text() == '{bad'
        for path, message in ((deep, 'deeply nested'), (dense, 'too complex')):
            open_file(path)
            expect(editor).to_have_value(path.read_text())
            expect(editor).to_be_visible()
            expect(page.locator('#json-notice')).to_contain_text(message)
            expect(preview).not_to_be_visible()
        for suffix in ('jsonl', 'ndjson'):
            path = work / ('records.' + suffix)
            open_file(path)
            expect(editor).to_be_visible()
            expect(editor).to_have_value(path.read_text())
            expect(page.locator('#table-preview')).not_to_be_visible()
        print('PASS: lazy list rendering, scroll retention, invalid JSON repair and plain-text fallbacks', flush=True)

        # Clean external changes refresh the view, while source stays exact.
        open_file(document)
        document.write_text('{"external":123}\n')
        expect(preview).to_contain_text('"external"')
        page.click('#json-source-button')
        editor.fill('{"draft":456}\n')
        document.write_text('{"external":789}\n')
        page.wait_for_function('activeTab().externalConflict')
        expect(editor).to_have_value('{"draft":456}\n')
        page.click('#json-preview-button')
        expect(preview).to_contain_text('"draft"')
        assert document.read_text() == '{"external":789}\n'
        page.evaluate('reloadTextFromDisk(activeTab(), true)')
        expect(preview).to_contain_text('789')
        for value in ('null', '"root string"', '[]', '{}'):
            scalar = work / ('scalar-' + str(len(value)) + '.json')
            scalar.write_text(value)
            open_file(scalar)
            page.click('#text-reload')
            expect(preview).to_have_text(value)
        open_file(document)
        # A full browser reload restores JSON view, rather than table view.
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('booted')
        open_file(document)
        expect(preview).to_contain_text('"external"')
        document.write_text(original)
        page.click('#text-reload')
        expect(preview).to_contain_text('Response analysis')
        for theme in ('light', 'dark'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            for width in (1440, 390, 320):
                page.set_viewport_size({'width': width, 'height': 900})
                expect(preview).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 960})
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-json-preview.png'))
        assert not errors, errors
        print('PASS: external reload, draft conflict protection, root values, restored JSON tabs, light/dark and narrow layouts; no browser errors', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
