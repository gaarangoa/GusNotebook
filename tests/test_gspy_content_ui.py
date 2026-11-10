"""Content authoring, live values, producing code, layouts and recovery UI."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    path = str(Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work' / 'science.gspy')
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        authenticate_browser(context, url)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.click('#tab-new')
        page.get_by_role('menuitem', name='Scientific notebook').click()
        page.fill('#ask-input', 'science.gspy')
        page.click('#ask-ok')
        page.wait_for_function("activeTab()?.format === 'gspy'")
        source = '''from IPython.display import display
import matplotlib.pyplot as plt
performance = {"estimate": 0.25, "ci_low": 0.25, "ci_high": 0.26}
fig, ax = plt.subplots(figsize=(3, 2))
ax.scatter([1, 2, 3], [2, 1, 3])
ax.set(xlabel='PC 1', ylabel='PC 2')
display(fig, metadata={"gusnotebook": {"result": "pca"}})
plt.close(fig)
'''
        response = context.request.post(url + 'api/cells', params={'notebook': path}, data={'cell_type': 'code', 'source': source})
        assert response.ok, response.text()
        cid = response.json()['id']
        page.wait_for_function('id => !!getCell(id)?.content', arg=cid)
        block = page.locator('#cell-' + cid)
        expect(block.locator('.gspy-code')).not_to_have_attribute('open', '')
        page.evaluate('id => openGspyContent(id)', cid)
        modal = page.locator('#gspy-dialog-back')
        modal.locator('#gspy-results').fill('performance')
        modal.locator('#gspy-layout').select_option('columns')
        modal.locator('#gspy-components [data-result]').fill('pca')
        modal.get_by_role('button', name='Add paragraph', exact=True).click()
        paragraph = modal.locator('.gspy-component-editor').last
        paragraph.locator('textarea').fill('## Results\nPerformance was {{estimate}}, with CI {{low}}–{{high}}.\n\nPC 1 and PC 2 are shown in the figure.')
        paragraph.get_by_role('button', name='Move component up').click()
        for name, field in [('estimate', 'estimate'), ('low', 'ci_low'), ('high', 'ci_high')]:
            modal.get_by_role('button', name='Add value', exact=True).click()
            row = modal.locator('.gspy-binding-editor').last
            row.locator('[data-name]').fill(name)
            row.locator('[data-result]').fill('performance')
            row.locator('[data-path]').fill(field)
            row.locator('[data-format]').fill('.2f')
        modal.get_by_role('button', name='Save content').click()
        expect(modal).not_to_be_visible()
        page.click('#workspace-more')
        page.get_by_role('menuitem', name='Run all blocks', exact=True).click()
        page.wait_for_function('id => getCell(id).content.run.state === "fresh"', arg=cid)
        expect(block.locator('.gspy-content')).to_contain_text('Performance was 0.25, with CI 0.25–0.26.')
        expect(block.locator('.gspy-content img')).to_be_visible()
        assert block.locator('.gspy-content').evaluate('el => getComputedStyle(el).gridTemplateColumns.split(" ").length') == 2
        assert block.locator('h2').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') > block.locator('.gspy-value').first.evaluate('el => parseFloat(getComputedStyle(el).fontSize)')
        expect(block.get_by_text('Supporting results changed.', exact=False)).to_be_visible()
        block.get_by_role('button', name='Mark reviewed', exact=True).click()
        expect(block.get_by_role('button', name='Mark reviewed', exact=True)).not_to_be_visible()
        first_run = page.evaluate('id => getCell(id).content.run.run_id', cid)

        block.locator('.gspy-value').first.focus()
        page.keyboard.press('Enter')
        expect(modal.locator('.gspy-source')).to_contain_text('performance =')
        expect(modal).to_contain_text(first_run)
        modal.get_by_role('button', name='Edit current code').click()
        expect(block.locator('.gspy-code')).to_have_attribute('open', '')
        assert page.evaluate('!document.getElementById("app").inert')

        def replace_source(text):
            response = context.request.patch(url + 'api/cells/' + cid, params={'notebook': path}, data={'source': text})
            assert response.ok, response.text()
            page.wait_for_function('args => getCell(args.id).source === args.source', arg={'id': cid, 'source': text})

        changed = source.replace('"estimate": 0.25', '"estimate": 0.35')
        replace_source(changed)
        page.wait_for_function('id => getCell(id).content.run.state === "stale"', arg=cid)
        expect(block.locator('.gspy-value').first).to_have_text('0.25')
        block.locator('.gspy-value').first.click()
        expect(modal).to_contain_text('current code differs')
        expect(modal.locator('.gspy-source')).to_contain_text('"estimate": 0.25')
        page.keyboard.press('Escape')
        page.evaluate('id => runCell(id)', cid)
        expect(block.locator('.gspy-value').first).to_have_text('0.35')
        expect(block.get_by_role('button', name='Mark reviewed', exact=True)).to_be_visible()

        replace_source("raise ValueError('intentional failure')")
        page.evaluate('id => runCell(id)', cid)
        expect(block.locator('.gspy-value').first).to_have_text('0.35')
        expect(block).to_contain_text('intentional failure')
        expect(block.locator('.gspy-content img')).to_be_visible()
        # Existing copy snapshots must use the producing source, even after a
        # failed edit. Stub only the OS clipboard write, inspect the real payload
        # and persisted copy receipt produced by the application.
        payload = page.evaluate('''async id => {
          const write = copyRichHtml;
          let html;
          copyRichHtml = async value => { html = value; };
          try { await copyCellProvenance(id); } finally { copyRichHtml = write; }
          const dom = new DOMParser().parseFromString(html, 'text/html');
          return JSON.parse(dom.querySelector('[data-gusnb-viz-source]').textContent);
        }''', cid)
        assert payload['producing_run_id']
        assert 'performance =' in payload['code']
        assert payload['output_mime'] == 'image/png'
        replace_source(changed)
        page.evaluate('id => runCell(id)', cid)
        response = context.request.post(url + 'api/kernel/restart', params={'notebook': path}, data={})
        assert response.ok, response.text()
        page.wait_for_function('id => getCell(id).content.run.state === "stale"', arg=cid)
        expect(block.locator('.gspy-value').first).to_have_text('0.35')
        page.evaluate('id => gspyRunThrough(id)', cid)
        page.wait_for_function('id => getCell(id).content.run.state === "fresh"', arg=cid)

        page.evaluate('id => openGspyContent(id)', cid)
        modal.locator('#gspy-layout').select_option('slide')
        modal.get_by_role('button', name='Save content').click()
        expect(block.locator('.gspy-slide')).to_be_visible()
        before = json.loads(Path(path).read_text())
        run_count = sum(e['kind'] == 'run_started' for e in before['metadata']['gusnotebook']['journal'])
        for theme in ('dark', 'light'):
            page.evaluate('theme => { document.documentElement.dataset.theme = theme; }', theme)
            page.screenshot(path=str(Path(tempfile.gettempdir()) / ('gusnb-gspy-' + theme + '.png')))
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('id => !!getCell(id)?.content', arg=cid)
        expect(block.locator('.gspy-slide')).to_be_visible()
        expect(block.locator('.gspy-value').first).to_have_text('0.35')
        after = json.loads(Path(path).read_text())
        assert sum(e['kind'] == 'run_started' for e in after['metadata']['gusnotebook']['journal']) == run_count
        assert any(e['kind'] == 'review' for e in after['metadata']['gusnotebook']['journal'])
        assert any(e.get('producing_run_id') == payload['producing_run_id'] for e in after['metadata']['gusnotebook']['journal'])
        assert not errors, errors
        browser.close()
    print('GSPY content, values, figures, layouts, review, failures and restart passed.')


if __name__ == '__main__':
    main()
