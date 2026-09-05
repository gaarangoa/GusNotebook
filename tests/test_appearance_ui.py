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
        header = page.locator('.app-header').bounding_box()
        toolbar = page.locator('#toolbar').bounding_box()
        assert header['height'] == 38
        assert header['x'] == toolbar['x'] and header['width'] == toolbar['width']
        for selector in ['#app', '#files', '#agent-pane', '.app-header']:
            assert page.locator(selector).bounding_box()['y'] == 0, selector
        for selector in ['#files', '#agent-pane']:
            assert page.locator(selector).bounding_box()['height'] == 960, selector
        assert page.locator('#tabs').bounding_box()['y'] == 0
        assert page.locator('#toolbar').bounding_box()['y'] == 38
        assert page.locator('#notebook-pane').bounding_box()['y'] <= 78
        assert page.locator('#nb-path, #nb-label').count() == 0
        file_row = page.locator('.file-row').first
        expect(file_row).to_have_css('font-size', '12px')
        assert file_row.bounding_box()['height'] == 24
        # Name-only Skills/Sessions keep creation and secondary actions accessible.
        assert page.locator('#skills svg, #sessions svg').count() == 0
        for section in ['skills', 'sessions']:
            assert page.locator('#' + section + ' .strip-head').bounding_box()['height'] == 26
        page.locator('#skills .strip-new').click()
        expect(page.locator('#skill-back')).to_be_visible()
        page.fill('#skill-name', 'Compact skill')
        page.fill('#skill-body', '# Reusable snippet\n\n```python\nprint("compact")\n```')
        page.locator('#skill-back .btn.primary').click()
        page.wait_for_function('skillList.some(s => s.id === "compact-skill")')
        page.locator('#skills .strip-head').press('Enter')
        skill = page.locator('.skill-row').filter(has_text='compact-skill')
        expect(skill).to_have_text('compact-skill')
        assert skill.bounding_box()['height'] == 26
        skill.click(button='right')
        page.get_by_role('menu', name='Skill actions').get_by_role('menuitem', name='Edit skill…').click()
        expect(page.locator('#skill-name')).to_have_value('compact-skill')
        page.keyboard.press('Escape')
        skill.press('F2')
        expect(page.locator('#skill-back')).to_be_visible()
        page.keyboard.press('Escape')
        page.locator('#sessions .strip-head').press('Enter')
        session = page.locator('.session-row.current')
        assert session.bounding_box()['height'] == 26
        session.press('Shift+F10')
        page.get_by_role('menu', name='Session actions').get_by_role('menuitem', name='Agent settings…').click()
        expect(page.locator('#sinstr-back')).to_be_visible()
        page.keyboard.press('Escape')
        session.click(button='right')
        page.get_by_role('menu', name='Session actions').get_by_role('menuitem', name='Close session…').click()
        expect(page.locator('#ask-back')).to_be_visible()
        page.keyboard.press('Escape')
        expect(session).to_be_visible()
        page.locator('#sessions .strip-new').click()
        expect(page.locator('#ask-back')).to_be_visible()
        page.keyboard.press('Escape')
        print('PASS: compact name-only lists retain creation, editing and keyboard context actions', flush=True)
        page.evaluate('selectCell(cells[1].id)')
        expect(page.locator('.cell.is-current')).to_have_css('border-left-width', '2px')
        expect(page.locator('.cell.is-current .cell-body')).to_have_css('border-left-width', '0px')
        page.screenshot(path=str(screenshots / 'light.png'))

        page.locator('#workspace-more').focus()
        page.keyboard.press('ArrowDown')
        expect(page.locator('#focus-toggle')).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.locator('#theme-toggle')).to_be_focused()
        page.keyboard.press('Enter')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        expect(page.locator('#workspace-more')).to_be_focused()
        expect(page.locator('#workspace-menu')).not_to_be_visible()
        page.click('#workspace-more')
        page.click('#theme-toggle')
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.click('#workspace-more')
        page.keyboard.press('Escape')
        expect(page.locator('#workspace-more')).to_have_attribute('aria-expanded', 'false')
        expect(page.locator('#workspace-more')).to_be_focused()
        page.click('#workspace-more')
        page.click('#tab-new')
        expect(page.locator('#workspace-menu')).not_to_be_visible()
        page.keyboard.press('Escape')

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
        page.click('#workspace-more')
        expect(page.locator('#nb-reload')).not_to_be_visible()
        page.keyboard.press('Escape')
        page.locator('#tabs .tab.active').focus()
        page.keyboard.press('ArrowLeft')
        page.wait_for_function('activeTab().kind === "notebook"')
        expect(page.locator('#tabs .tab.active')).to_be_focused()
        page.click('#workspace-more')
        page.click('#focus-toggle')
        expect(page.locator('#focus-toggle')).to_have_attribute('aria-checked', 'true')
        assert page.evaluate('document.getElementById("files").inert && document.getElementById("agent-pane").inert')
        page.click('#workspace-more')
        page.click('#focus-toggle')
        assert not page.evaluate('document.getElementById("files").inert')
        for width in [1024, 768, 390, 320]:
            page.set_viewport_size({'width': width, 'height': 844})
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('#tabs').bounding_box()['width'] > 100
            for control in ['#settings-button', '#workspace-more', '#tab-new']:
                box = page.locator(control).bounding_box()
                assert 0 <= box['x'] and box['x'] + box['width'] <= width, (control, box)
            page.click('#workspace-more')
            box = page.locator('#workspace-menu').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width
            page.keyboard.press('Escape')
            page.wait_for_function("""() => {
              const strip = document.getElementById('tabs').getBoundingClientRect();
              const chosen = document.querySelector('#tabs .tab.active').getBoundingClientRect();
              const plus = document.getElementById('tab-new').getBoundingClientRect();
              return chosen.left >= strip.left - 1 && chosen.right <= plus.left + 1;
            }""")
            page.click('#toggle-files')
            expect(page.locator('#toggle-files')).to_have_attribute('aria-expanded', 'true')
            assert page.evaluate('!document.getElementById("files").inert')
            assert page.locator('#files').bounding_box()['y'] == 0
            page.keyboard.press('Escape')
            expect(page.locator('#toggle-files')).to_be_focused()
            if width < 820:
                page.click('#toggle-terminal')
                assert page.evaluate('!document.getElementById("agent-pane").inert')
                assert page.locator('#agent-pane').bounding_box()['y'] == 0
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
        # Rename through the relocated tab menu, preserving drafts and live variables.
        page.set_viewport_size({'width': 1440, 'height': 960})
        page.evaluate("""() => {
          const view = cmViews.get(cells[1].id);
          view.dispatch({changes: {from: view.state.doc.length, insert: '\\n# retained after rename'}});
        }""")
        page.locator('#tabs .tab.active').click(button='right')
        page.locator('#file-ctx').get_by_role('menuitem', name='Rename…', exact=True).click()
        expect(page.locator('#ask-input')).to_have_value(notebook.name)
        page.fill('#ask-input', "compact review's")
        page.click('#ask-ok')
        page.wait_for_function("activeTab().name === \"compact review's.ipynb\"")
        renamed = work / "compact review's.ipynb"
        assert renamed.is_file() and not notebook.exists()
        assert nbformat.read(renamed, as_version=4).cells[1].source.endswith('# retained after rename')
        output = page.evaluate("""async () => {
          const id = cells[2].id;
          document.getElementById('ed-' + id).value = 'print(answer)';
          await runCell(id);
          return document.getElementById('out-' + id).innerText;
        }""")
        assert '42' in output, output
        page.locator('#tabs .tab.active').press('F2')
        expect(page.locator('#ask-input')).to_have_value(renamed.name)
        page.keyboard.press('Escape')
        page.click('#workspace-more')
        page.click('#nb-reload')
        page.wait_for_function("!document.getElementById('workspace-more').matches('[aria-expanded=true]')")
        expect(page.locator('#tabs .tab.active')).to_contain_text(renamed.name)
        print('PASS: tab-menu rename saves drafts and retains the live kernel; F2 and reload work', flush=True)
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
