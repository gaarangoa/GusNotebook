"""Cell requests, decisions, and the copy → HTML report trail in a disposable app."""

import json
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
    notebook = work / 'cell audit.ipynb'
    report = work / 'report.html'
    report.write_text('<!doctype html><html><body><h1>Analysis report</h1><p>Results follow.</p></body></html>')
    nbformat.write(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('value = 1'),
                                                 nbformat.v4.new_code_cell('untouched = True')]), notebook)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={'width': 1440, 'height': 960},
                                      permissions=['clipboard-read', 'clipboard-write'])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        authenticate_browser(context, url)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.evaluate('path => openFile(path)', str(notebook))
        page.wait_for_function('cells.length === 2 && cmViews.size === 2')
        path, cell, untouched, session = page.evaluate('[active, cells[0].id, cells[1].id, currentSession]')
        page.evaluate('id => api("/api/focus" + nbq(), {method:"POST", body:JSON.stringify({cell_id:id})})', cell)
        headers = {'X-Session-Id': session, 'X-Terminal-Id': 'history-test-agent'}
        first = 'Use medians and exclude missing observations.\n' + 'Retain this full instruction. ' * 30
        second = 'Keep the comparison descriptive. <img src=x onerror=alert(1)>'

        def send_request(prompt, source):
            response = context.request.post(url + 'api/prompt', headers=headers, data={'prompt': prompt})
            assert response.ok, response.text()
            response = context.request.patch(url + 'api/cells/' + cell,
                params={'notebook': path}, headers=headers, data={'source': source, 'undoable': True})
            assert response.ok, response.text()

        send_request(first, 'value = 2')
        source = "from IPython.display import display, HTML\ndisplay(HTML('<table><tr><th>Median</th></tr><tr><td>2</td></tr></table>'))"
        send_request(second, source)
        strip = page.locator(f'.cell[data-id="{cell}"] .ai-prompt.claude')
        expect(strip.locator('.pt')).to_have_text(second)
        expect(strip).to_contain_text('2 requests')
        history_button = page.locator(f'.cell[data-id="{cell}"] .gutter-acts').get_by_role('button', name='Open cell history', exact=True)
        history_button.press('Enter')
        expect(page.locator('#cell-history-back')).to_be_visible()
        requests = page.locator('.cell-history-event[data-event-kind="request"]')
        expect(requests).to_have_count(2)
        expect(requests.last.locator('.cell-history-text')).to_have_text(first)
        assert page.locator('#cell-history-list img').count() == 0
        expect(page.locator('#cell-history-list')).to_contain_text('Source changes')
        note = 'Preference: use medians; this is an exploratory comparison, not a causal claim.'
        page.fill('#cell-history-note', note)
        page.click('#cell-history-note-save')
        expect(page.locator('.cell-history-event[data-event-kind="note"]')).to_contain_text(note)
        with page.expect_download() as download:
            page.get_by_role('button', name='Download log', exact=True).click()
        exported = json.loads(Path(download.value.path()).read_text())
        assert exported['cell_id'] == cell and exported['notebook'] == path
        assert any(e.get('prompt') == first for e in exported['events'])
        screenshot = Path(tempfile.gettempdir()) / 'gusnb-cell-history.png'
        page.screenshot(path=str(screenshot))
        page.keyboard.press('Escape')
        expect(history_button).to_be_focused()
        page.evaluate('id => runCell(id)', cell)
        page.frame_locator('iframe[data-output-frame]').locator('table').wait_for()
        page.locator(f'.cell[data-id="{cell}"]').get_by_role('button', name='Copy provenance snapshot', exact=False).click()
        page.wait_for_function('id => getCell(id).history_summary.copies === 1', arg=cell)
        copied = page.evaluate("""async () => {
          const items = await navigator.clipboard.read();
          return {html: await (await items[0].getType('text/html')).text(),
                  source: await (await items[0].getType('text/plain')).text()};
        }""")
        assert 'data-gusnb-snapshot=' in copied['html']
        assert 'Retain this full instruction.' in copied['source']
        page.evaluate('path => openFile(path)', str(report))
        preview = page.frame_locator('#html-preview-frame')
        preview.locator('body[contenteditable="true"]').wait_for()
        preview.locator('body').evaluate("""(body, copied) => {
          const range = document.createRange(); range.selectNodeContents(body); range.collapse(false);
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
          const transfer = new DataTransfer(); transfer.setData('text/html', copied.html);
          transfer.setData('text/plain', copied.source);
          body.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:transfer}));
        }""", copied)
        preview.locator('figure[data-gusnb-snapshot]').wait_for()
        page.wait_for_function('activeTab().dirty')
        page.evaluate('saveText()')
        page.wait_for_function('!activeTab().dirty')
        assert 'data-gusnb-snapshot=' in report.read_text()
        assert 'Retain this full instruction.' in report.read_text()
        preview.frame_locator('.gusnb-viz-frame').locator('table').wait_for()
        page.evaluate('path => openFile(path)', path)
        page.wait_for_function('arg => active === arg.path && getCell(arg.cell)?.history_summary.exports === 1',
                               arg={'path': path, 'cell': cell})
        strip.click()
        expect(page.locator('.cell-history-event[data-event-kind="export"]')).to_contain_text('report.html')
        expect(page.locator('.cell-history-event[data-event-kind="run"]')).to_have_count(1)
        page.keyboard.press('Escape')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.evaluate('path => openFile(path)', path)
        page.wait_for_function('id => typeof cells !== "undefined" && getCell(id)', arg=cell)
        strip.click()
        expect(page.locator('.cell-history-event[data-event-kind="export"]')).to_have_count(1)
        expect(page.locator('.cell-history-event[data-event-kind="note"]')).to_contain_text(note)
        page.keyboard.press('Escape')
        untouched_cell = page.locator(f'.cell[data-id="{untouched}"]')
        expect(untouched_cell.locator('.cell-body')).not_to_contain_text('History')
        untouched_cell.hover()
        untouched_cell.locator('.gutter-acts').get_by_role('button', name='Open cell history', exact=True).click()
        expect(page.locator('#cell-history-list')).not_to_contain_text(first)
        expect(page.locator('#cell-history-list')).not_to_contain_text(note)
        page.keyboard.press('Escape')
        # Failed clipboard writes do not become successful copy receipts.
        page.evaluate('() => { window.copyRichHtml = async () => { throw new Error("Clipboard denied"); }; }')
        page.evaluate('id => copyCellProvenance(id)', cell)
        assert page.evaluate('id => getCell(id).history_summary.copies', cell) == 1
        assert not errors, errors
        print('PASS: full cell requests, notes, safe rendering, log download, real clipboard, HTML paste/save, reload, isolation and failed-copy handling', flush=True)
        print('Screenshot: ' + str(screenshot), flush=True)
        context.close()
        browser.close()


if __name__ == '__main__':
    main()
