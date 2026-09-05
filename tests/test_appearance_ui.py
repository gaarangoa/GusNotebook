"""Appearance, responsive layout and keyboard workflows in a disposable app."""

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
    notebook = work / "design review's.ipynb"
    table = '<table><tr><th>Cohort</th><th>Response</th></tr><tr><td>Treatment</td><td>67%</td></tr></table>'
    nbformat.write(nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_markdown_cell('# Experiment review\nCompare cohorts and record the results.'),
        nbformat.v4.new_code_cell('answer = 42\nprint(answer)', execution_count=1, outputs=[
            nbformat.v4.new_output('display_data', data={'text/html': table})]),
        nbformat.v4.new_code_cell('missing_name', outputs=[nbformat.v4.new_output(
            'error', ename='NameError', evalue='missing_name', traceback=['NameError: missing_name'])]),
    ]), notebook)
    (work / 'notes.md').write_text('Review notes\n')
    screenshots = Path(tempfile.gettempdir()) / 'gusnb-appearance'
    screenshots.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960}, color_scheme='light')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        authenticate_browser(page.context, url)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.evaluate('path => openFile(path)', str(notebook))
        page.wait_for_function('cells.length === 3 && cmViews.size === 2')
        page.evaluate('path => browse(path)', str(work))
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.frame_locator('iframe[data-output-frame]').locator('table').wait_for()
        page.screenshot(path=str(screenshots / 'light.png'))

        # Real edits and a real terminal must survive appearance changes in place.
        page.evaluate('''() => {
          window.reviewId = cells[1].id;
          window.reviewView = cmViews.get(reviewId);
          reviewView.dispatch({changes: {from: reviewView.state.doc.length, insert: '\\n# edited'},
            selection: {anchor: 4}});
          window.reviewUndo = CM.undoDepth(reviewView.state);
          window.reviewFrame = document.querySelector('iframe[data-output-frame]');
        }''')
        page.evaluate('path => openTerminal(path, "shell")', str(work))
        page.wait_for_function('terms.length === 1 && terms[0].ws.readyState === 1')
        page.evaluate('window.reviewTerminal = terms[0].term; terms[0].ws.send("echo APPEARANCE_OK\\r")')
        page.click('#settings-button')
        expect(page.locator('#settings-back')).to_be_visible()
        expect(page.locator('#settings-back')).to_have_class('modal-back on')
        page.select_option('#set-theme', 'dark')
        page.select_option('#set-density', 'compact')
        page.fill('#set-font-size', '16')
        page.locator('#set-font-size').press('Tab')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert page.evaluate('cmViews.get(reviewId) === reviewView && CM.undoDepth(reviewView.state) === reviewUndo && reviewView.state.selection.main.anchor === 4')
        assert page.evaluate('terms[0].term === reviewTerminal && reviewTerminal.options.fontSize === 16')
        assert page.evaluate('document.querySelector("iframe[data-output-frame]") === reviewFrame')
        page.frame_locator('iframe[data-output-frame]').locator('html').evaluate('el => window.tableIdentity = el')
        page.screenshot(path=str(screenshots / 'settings-dark.png'))
        page.keyboard.press('Escape')
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        expect(page.locator('#settings-button')).to_be_focused()
        assert page.evaluate('AppAppearance.get().density === "comfortable" && reviewTerminal.options.fontSize === 14')
        print('PASS: preview/cancel preserves editor identity, selection, undo, table frames and live terminals', flush=True)

        page.click('#settings-button')
        expect(page.locator('#settings-back')).to_be_visible()
        page.locator('#settings-appearance-tab').press('ArrowRight')
        expect(page.locator('#settings-notebook-tab')).to_be_focused()
        expect(page.locator('#settings-notebook')).to_be_visible()
        page.locator('#settings-notebook-tab').press('ArrowRight')
        expect(page.locator('#set-claude')).to_be_visible()
        page.fill('#set-claude', 'Explain changes concisely.')
        page.locator('#settings-agents details').filter(has=page.locator('#set-restrict')).locator('summary').click()
        page.check('#set-restrict input[value=no_execute]')
        page.click('#settings-appearance-tab')
        page.select_option('#set-theme', 'dark')
        page.select_option('#set-density', 'compact')
        page.fill('#set-font-size', '16')
        page.locator('#set-font-size').press('Tab')
        page.click('#settings-save')
        expect(page.locator('#settings-back')).not_to_be_visible()
        assert page.evaluate('settingsData.settings.claude_instructions') == 'Explain changes concisely.'
        assert page.evaluate('settingsData.settings.claude_restrictions.no_execute') is True
        assert page.evaluate('CM.undoDepth(reviewView.state) === reviewUndo && reviewView.state.doc.toString().endsWith("# edited")')
        expect(page.frame_locator('iframe[data-output-frame]').locator('html')).to_have_css('color-scheme', 'dark')
        assert page.frame_locator('iframe[data-output-frame]').locator('html').evaluate('el => window.tableIdentity === el')
        expect(page.locator('.left')).to_have_css('background-color', 'rgb(24, 27, 34)')
        page.screenshot(path=str(screenshots / 'dark.png'))

        # Resize from both input methods, then verify browser-local persistence.
        page.locator('#file-splitter').focus()
        page.keyboard.press('ArrowRight')
        assert page.evaluate('layoutPrefs.filesWidth') == 256
        splitter = page.locator('#splitter').bounding_box()
        page.mouse.move(splitter['x'] + 3, splitter['y'] + 80)
        page.mouse.down()
        page.mouse.move(splitter['x'] - 45, splitter['y'] + 80)
        page.mouse.up()
        widths = page.evaluate('[layoutPrefs.filesWidth, layoutPrefs.termWidth]')
        assert widths[1] > 390, widths
        page.evaluate('() => saveActiveDocument()')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.wait_for_function('cells.length === 3 && cmViews.size === 2 && terms.length === 1')
        assert page.evaluate('[layoutPrefs.filesWidth, layoutPrefs.termWidth]') == widths
        assert page.evaluate('AppAppearance.get()') == {'theme': 'dark', 'density': 'compact', 'fontSize': 16}
        print('PASS: saved appearance, panel widths and agent settings persist after reload', flush=True)

        # Save errors stay visible and do not discard the appearance preview.
        page.click('#settings-button')
        expect(page.locator('#settings-back')).to_be_visible()
        page.route('**/api/settings', lambda route: route.fulfill(status=500, json={'error': 'test save failure'}) if route.request.method == 'POST' else route.continue_())
        page.select_option('#set-theme', 'light')
        page.click('#settings-save')
        expect(page.locator('#settings-error')).to_contain_text('Cannot save settings')
        expect(page.locator('#settings-save')).to_be_enabled()
        page.keyboard.press('Escape')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.unroute('**/api/settings')
        page.click('#settings-button')
        expect(page.locator('#settings-back')).to_be_visible()
        page.select_option('#set-theme', 'system')
        page.click('#settings-save')
        expect(page.locator('#settings-back')).not_to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.emulate_media(color_scheme='dark')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')

        # Keyboard creation menu, modal focus wrapping, environment inspection.
        page.locator('#tab-new').focus()
        page.keyboard.press('Enter')
        expect(page.locator('#new-menu [role=menuitem]').first).to_be_focused()
        for _ in range(3):
            page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        expect(page.locator('#environment-back')).to_be_visible()
        page.click('#environment-packages-tab')
        page.wait_for_function('environmentState.info && environmentState.info.packages.length')
        expect(page.locator('#environment-package-rows')).to_contain_text('ipykernel')
        page.screenshot(path=str(screenshots / 'packages-dark.png'))
        page.evaluate('focusableControls(topModal()).at(-1).focus()')
        page.keyboard.press('Tab')
        assert page.evaluate('document.activeElement === focusableControls(topModal())[0]')
        page.keyboard.press('Escape')
        expect(page.locator('#environment-back')).not_to_be_visible()
        print('PASS: settings failure handling, system theme, keyboard menus and modal focus', flush=True)

        page.evaluate('path => openFile(path)', str(work / 'notes.md'))
        page.wait_for_function('activeTab().kind === "text"')
        page.locator('#tabs .tab.active').focus()
        page.keyboard.press('ArrowLeft')
        page.wait_for_function('activeTab().kind === "notebook"')
        expect(page.locator('#tabs .tab.active')).to_be_focused()
        page.click('#focus-toggle')
        expect(page.locator('#focus-toggle')).to_have_attribute('aria-pressed', 'true')
        assert page.evaluate('document.getElementById("files").inert && document.getElementById("agent-pane").inert')
        page.click('#focus-toggle')
        assert not page.evaluate('document.getElementById("files").inert')
        for width in [1024, 768, 390]:
            page.set_viewport_size({'width': width, 'height': 844})
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
            page.click('#toggle-files')
            expect(page.locator('#toggle-files')).to_have_attribute('aria-expanded', 'true')
            assert page.evaluate('!document.getElementById("files").inert')
            page.keyboard.press('Escape')
            expect(page.locator('#toggle-files')).to_be_focused()
            if width < 820:
                page.click('#toggle-terminal')
                assert page.evaluate('!document.getElementById("agent-pane").inert')
                page.keyboard.press('Escape')
            page.screenshot(path=str(screenshots / f'dark-{width}.png'))
        page.click('#settings-button')
        expect(page.locator('#settings-back')).to_be_visible()
        assert page.locator('.settings-modal').bounding_box()['width'] <= 390
        page.keyboard.press('Escape')
        print('PASS: notebook tabs, focus mode, responsive drawers and narrow settings', flush=True)

        # The primary Run action still executes the selected code cell.
        page.evaluate('selectCell(cells[1].id)')
        page.locator('#toolbar').get_by_role('button', name='Run selected cell', exact=False).click()
        expect(page.locator('#out-' + page.evaluate('cells[1].id'))).to_contain_text('42', timeout=60000)
        # The unlock page uses the same saved theme before the app has loaded.
        page.context.clear_cookies()
        page.goto(url, wait_until='domcontentloaded')
        expect(page.locator('.unlock-card')).to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert not errors, errors
        browser.close()
        print('PASS: primary execution and no JavaScript errors; screenshots in ' + str(screenshots), flush=True)


if __name__ == '__main__':
    main()
