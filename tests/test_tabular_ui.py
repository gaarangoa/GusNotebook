"""Table previews in an isolated server: source, controls, formats and limits."""

import json
import os
from pathlib import Path
import tempfile

from openpyxl import Workbook
import pyarrow as arrow
import pyarrow.feather as feather
import pyarrow.parquet as parquet
from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    csv = work / 'data # α.csv'
    original = 'Name,Value,Note\nA,10,"first\nsecond"\nB,2,"<img src=x onerror=window.tableXss=true>"\n'
    csv.write_text(original)
    tsv = work / 'data.tsv'
    tsv.write_text('Name\tValue\nC\t3\n')
    semicolon = work / 'semi.csv'
    semicolon.write_text('Name;Value\nD;4\n')
    wide = work / 'wide.csv'
    wide.write_text(','.join(f'Column {i}' for i in range(120)) + '\n' + ','.join('value' for _ in range(120)))
    capped = work / 'snapshot.csv'
    capped.write_text('Name,Value\n' + ''.join(f'Row {i},{i}\n' for i in range(6000)))
    large = work / 'large.csv'
    large.write_text('x' * (2 * 1024 * 1024 + 1))
    json_file = work / 'records.json'
    json_file.write_text(json.dumps([{'Name': 'E', 'Value': 5}, {'Name': 'F', 'Nested': {'a': 1}}]))
    records = work / 'records.jsonl'
    records.write_text('{"Name":"G","Value":6}\n{"Name":"H","Value":7}\n')
    ndjson = work / 'records.ndjson'
    ndjson.write_text(records.read_text())
    invalid = work / 'invalid.json'
    invalid.write_text('{bad')
    table = arrow.table({'Name': ['I', 'J'], 'Value': [8, 9]})
    parquet_file, feather_file = work / 'data.parquet', work / 'data.feather'
    parquet.write_table(table, parquet_file)
    feather.write_feather(table, feather_file)
    book = work / 'book.xlsx'
    workbook = Workbook()
    workbook.active.title = 'Results'
    workbook.active.append(['Name', 'Value'])
    workbook.active.append(['K', 10])
    workbook.create_sheet('Other # α').append(['Other', 'Data'])
    workbook['Other # α'].append(['L', 11])
    workbook.save(book)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')

        def open_file(path):
            page.evaluate('path => openFile(path)', str(path))
            expect(page.locator('#table-preview')).to_be_visible()
            page.wait_for_function('!activeTab().tableLoading')

        open_file(csv)
        expect(page.locator('#text-editor')).not_to_be_visible()
        expect(page.locator('#table-grid tbody tr')).to_have_count(2)
        expect(page.locator('#table-grid tbody tr').first.locator('td').nth(2)).to_have_text('first\nsecond')
        expect(page.locator('#table-grid img')).to_have_count(0)
        assert not page.evaluate('!!window.tableXss')
        page.get_by_role('button', name='Value', exact=True).click()
        expect(page.locator('#table-grid tbody tr').first.locator('td').first).to_have_text('B')
        page.get_by_role('button', name='Value ↑', exact=True).click()
        expect(page.locator('#table-grid tbody tr').first.locator('td').first).to_have_text('A')
        page.locator('#table-search').fill('B')
        expect(page.locator('#table-grid tbody tr')).to_have_count(1)
        page.locator('#table-search').fill('')
        page.locator('#table-header').uncheck()
        expect(page.locator('#table-grid tbody tr')).to_have_count(3)
        page.locator('#table-header').check()
        assert csv.read_text() == original
        page.click('#table-source-button')
        draft = original + 'Draft,3,unsaved\n'
        page.locator('#text-editor').fill(draft)
        page.click('#table-preview-button')
        expect(page.locator('#table-grid tbody tr')).to_have_count(3)
        assert csv.read_text() == original
        page.click('#text-save')
        page.wait_for_function('!activeTab().dirty && !activeTab().saveInFlight')
        assert csv.read_text() == draft
        # Clean external updates refresh the table through the existing poll.
        csv.write_text('Name,Value\nExternal,99\n')
        expect(page.locator('#table-grid tbody tr')).to_have_count(1)
        expect(page.locator('#table-grid tbody tr td').first).to_have_text('External')
        print('PASS: CSV quotes, safe cells, numeric sorting, filtering, headers, unsaved preview, save and external reload', flush=True)

        for path, value in ((tsv, 'C'), (semicolon, 'D'), (json_file, 'E'), (records, 'G'), (ndjson, 'G')):
            open_file(path)
            expect(page.locator('#table-grid tbody tr').first.locator('td').first).to_have_text(value)
            expect(page.locator('#table-source-button')).to_be_visible()
        open_file(semicolon)
        page.locator('#table-delimiter').select_option(',')
        expect(page.locator('#table-summary')).to_contain_text('1 columns')
        page.locator('#table-delimiter').select_option('auto')
        expect(page.locator('#table-summary')).to_contain_text('2 columns')
        open_file(invalid)
        expect(page.locator('#table-notice')).not_to_be_empty()
        page.click('#table-source-button')
        page.locator('#text-editor').fill('[{"fixed":42}]')
        page.click('#table-preview-button')
        expect(page.locator('#table-grid tbody tr td').first).to_have_text('42')
        assert invalid.read_text() == '{bad'
        print('PASS: TSV, delimiter selection, JSON, JSON Lines, NDJSON, and invalid-source recovery', flush=True)

        for path in (parquet_file, feather_file, book):
            open_file(path)
            expect(page.locator('#table-source-button')).not_to_be_visible()
            expect(page.locator('#text-save')).not_to_be_visible()
            expect(page.locator('#text-status')).to_have_text('read-only preview')
        page.locator('#table-sheet').select_option('Other # α')
        expect(page.locator('#table-grid tbody tr td').first).to_have_text('L')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('booted')
        expect(page.locator('#table-preview')).to_be_visible()
        expect(page.locator('#table-sheet')).to_be_visible()
        parquet.write_table(arrow.table({'Name': ['Refreshed'], 'Value': [12]}), parquet_file)
        open_file(parquet_file)
        page.click('#text-reload')
        expect(page.locator('#table-grid tbody tr td').first).to_have_text('Refreshed')
        print('PASS: Parquet, Feather, read-only Excel sheets, restored tabs and binary reload', flush=True)

        open_file(capped)
        expect(page.locator('#table-notice')).to_contain_text('first 5,000 rows')
        expect(page.locator('#table-grid tbody tr')).to_have_count(50)
        page.click('#table-next')
        expect(page.locator('#table-page')).to_have_text('2 / 100')
        expect(page.locator('#table-grid tbody tr td').first).to_have_text('Row 50')
        page.locator('#table-search').fill('Row 5999')
        expect(page.locator('#table-summary')).to_contain_text('0 matches')
        page.locator('#table-search').fill('')
        result = page.evaluate('''async path => {
          const response = await fetch(BASE + '/api/open', {method:'POST',
            headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})});
          return {status:response.status, body:await response.json()};
        }''', str(large))
        assert result['status'] == 400 and 'too large' in result['body']['error']
        assert not page.evaluate('path => tabs.some(t => t.path === path)', str(large))
        open_file(wide)
        expect(page.locator('#table-notice')).to_contain_text('first 100 columns')
        expect(page.locator('#table-grid thead th')).to_have_count(101)
        for theme in ('light', 'dark'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            for width in (1440, 390, 320):
                page.set_viewport_size({'width': width, 'height': 900})
                page.wait_for_timeout(250)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-table-preview.png'))
        assert not errors, errors
        print('PASS: bounded snapshots, pagination, oversize refusal, wide tables, mobile/light/dark layouts, no browser errors', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
