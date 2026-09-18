"""Live shell context and highlighting in the browser, using disposable PTYs."""

import os
from pathlib import Path
import shlex
import shutil
import tempfile
import venv

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    os.environ['SHELL'] = shutil.which('zsh') or shutil.which('bash')
    if os.environ.get('GUSNOTEBOOK_ISOLATED_TEST') != '1':
        with tempfile.TemporaryDirectory() as config:
            os.environ.update(ZDOTDIR=config, NO_COLOR='')
            rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = (Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work').resolve()
    folder = work / 'folder ; 100% <text>'
    folder.mkdir()
    environment = work / 'analysis env'
    venv.EnvBuilder(with_pip=False).create(environment)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        authenticate_browser(page.context, url)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.evaluate('path => openTerminal(path, "shell")', str(work))
        page.wait_for_function('terms.length === 1 && terms[0].ws.readyState === 1')
        header = page.locator('.term-host.on .term-context')
        directory = header.locator('.term-context-directory')
        env = header.locator('.term-context-env')
        expect(directory).to_have_text(str(work))
        expect(header.locator('.term-context-user')).not_to_be_empty()
        rows = page.locator('.term-host.on .xterm-rows')
        expect(rows).to_contain_text('>')
        assert str(work) not in rows.inner_text()

        page.evaluate('terms[0].term.focus()')
        page.keyboard.type('echo --example "hello" .')
        expect(rows).to_contain_text('hello')
        if Path(os.environ['SHELL']).name == 'zsh':
            for theme in ('light', 'dark'):
                page.evaluate('theme => AppAppearance.update({theme})', theme)
                # xterm may split a word into several spans; group by color.
                page.wait_for_function('''() => {
                  const colors = {};
                  for (const span of document.querySelectorAll('.term-host.on .xterm-rows span')) {
                    const color = getComputedStyle(span).color;
                    colors[color] = (colors[color] || '') + span.textContent;
                  }
                  const groups = ['echo', '--example', 'hello'].map(word =>
                    Object.keys(colors).find(color => colors[color].includes(word)));
                  return groups.every(Boolean) && new Set(groups).size === 3;
                }''', timeout=5000)
        page.evaluate('terms[0].term.focus()')
        page.keyboard.press('Control+c')
        command = 'cd ' + shlex.quote(str(folder)) + '; source ' + shlex.quote(str(environment / 'bin/activate'))
        page.keyboard.type(command)
        page.keyboard.press('Enter')
        expect(directory).to_have_text(str(folder))
        expect(env).to_have_text('(analysis env)')
        page.keyboard.type('clear')
        page.keyboard.press('Enter')
        expect(rows).not_to_contain_text('source')
        assert 'analysis env' not in rows.inner_text()
        expect(directory).to_have_text(str(folder))

        first_id = page.evaluate('terms[0].id')
        page.evaluate('path => openTerminal(path, "shell")', str(work))
        expect(directory).to_have_text(str(work))
        page.evaluate('id => focusTerm(id)', first_id)
        expect(directory).to_have_text(str(folder))
        expect(env).to_have_text('(analysis env)')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('typeof terms !== "undefined" && terms.length === 2 && terms.every(t => t.ws.readyState === 1)')
        page.evaluate('id => focusTerm(id)', first_id)
        expect(directory).to_have_text(str(folder))
        expect(env).to_have_text('(analysis env)')
        page.evaluate('AppAppearance.update({theme: "light"})')
        expect(page.locator('#terminal-stack')).to_have_css('border-radius', '0px')
        expect(page.locator('#agent-pane')).to_have_css('background-color', 'rgb(255, 255, 255)')
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-terminal-context.png'))
        page.evaluate('terms.find(t => t.id === activeTerm).term.focus()')
        page.keyboard.type('deactivate')
        page.keyboard.press('Enter')
        expect(env).to_be_hidden()
        assert not errors, errors
        browser.close()
        print('PASS: compact prompts, live directory/environment header, colors, tab switching, and reload')


if __name__ == '__main__':
    main()
