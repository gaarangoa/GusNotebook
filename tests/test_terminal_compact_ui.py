"""Agent blank rows collapse without changing PTY input, selection, or scrollback."""

import os
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


# A local stand-in for each CLI. It emits the user's example and accepts real
# terminal input; no agent account, network request, or inference is involved.
AGENT = r'''
import os, sys, tty
tty.setraw(0)
text = "╰──────────────────────────╯\r\n\r\n  Tip: Use /fast.\r\n\r\n• You have 3 resets available.\r\nRun /usage to use one.\r\n\r\n\r\n› hellow\r\n\r\n\r\n• Hello! What would you like?\r\n\r\n  done 8:30 PM\r\n\r\n› "
os.write(1, ('\x1b[2J\x1b[H' + text).encode())
draft = bytearray()
while True:
    data = os.read(0, 1)
    if not data or data == b'\x03': break
    if data in (b'\x7f', b'\x08'):
        if draft:
            draft.pop()
            os.write(1, b'\b \b')
    elif data == b'\r':
        os.write(1, b'\r\n\r\nAgent received: ' + draft + '\r\n\r\n› '.encode())
        draft.clear()
    else:
        draft.extend(data)
        os.write(1, data)
'''


def main():
    if os.environ.get('GUSNOTEBOOK_ISOLATED_TEST') != '1':
        with tempfile.TemporaryDirectory(prefix='gusnb-fake-agents-') as directory:
            # Terminal startup prepends uv's directory to PATH; keep that
            # directory inside this fixture so no installed CLI can win.
            from gusnotebook.environments import uv_binary
            uv = uv_binary()
            if uv:
                uv_link = Path(directory) / 'uv'
                uv_link.symlink_to(uv)
                os.environ['GUSNOTEBOOK_UV'] = str(uv_link)
            for name in ('codex', 'claude'):
                executable = Path(directory) / name
                executable.write_text('#!' + sys.executable + '\n' + AGENT)
                executable.chmod(0o755)
            os.environ['PATH'] = directory + os.pathsep + os.environ['PATH']
            rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        authenticate_browser(page.context, url)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        for kind in ('codex', 'claude'):
            page.evaluate('kind => openTerminal(null, kind)', kind)
            page.wait_for_selector('.term-host.on .compact-agent-rows')
            rows = page.locator('.term-host.on .xterm-rows > div')
            expect(rows.filter(has_text='› hellow')).to_have_count(1)
            page.wait_for_function('document.querySelectorAll(".term-host.on .compact-blank-row").length > 4')
            usage = rows.filter(has_text='Run /usage to use one.')
            hello = rows.filter(has_text='› hellow')
            answer = rows.filter(has_text='• Hello!')
            # Multiple empty lines become a single small 4px gap.
            assert abs(hello.bounding_box()['y'] - (usage.bounding_box()['y'] + usage.bounding_box()['height']) - 4) <= 1
            assert abs(answer.bounding_box()['y'] - (hello.bounding_box()['y'] + hello.bounding_box()['height']) - 4) <= 1

            # Mouse selection must hit the displayed word, not its old grid row.
            box = hello.bounding_box()
            cell_width = page.evaluate('findTerm(activeTerm).term._core._renderService.dimensions.css.cell.width')
            page.mouse.dblclick(box['x'] + cell_width * 4, box['y'] + box['height'] / 2)
            assert page.evaluate('findTerm(activeTerm).term.getSelection()') == 'hellow'
            page.wait_for_function('''() => {
              const host = findTerm(activeTerm).host;
              const word = [...host.querySelectorAll('.xterm-rows > div')].find(r => r.textContent.includes('› hellow'));
              const selection = host.querySelector('.xterm-selection > div');
              return selection && Math.abs(selection.getBoundingClientRect().top - word.getBoundingClientRect().top) <= 1;
            }''')

            page.evaluate('findTerm(activeTerm).term.clearSelection(); findTerm(activeTerm).term.focus()')
            page.keyboard.type('draftx')
            page.keyboard.press('Backspace')
            expect(page.locator('.term-host.on .xterm-rows > div:has(.xterm-cursor)')).to_contain_text('draft')
            page.wait_for_function('''() => {
              const t = findTerm(activeTerm).term;
              return Math.abs(t.textarea.getBoundingClientRect().top - t.element.querySelector('.xterm-cursor').getBoundingClientRect().top) <= 1;
            }''')
            page.keyboard.press('Enter')
            expect(rows.filter(has_text='Agent received: draft')).to_have_count(1)
            expect(rows.filter(has_text='Agent received: draftx')).to_have_count(0)

            # Fullscreen apps retain their uncompressed grid.
            page.evaluate('''() => new Promise(resolve => findTerm(activeTerm).term.write(
              '\\x1b[?1049h\\x1b[2J\\x1b[Htop\\r\\n\\r\\nbottom', resolve))''')
            page.wait_for_function('document.querySelectorAll(".term-host.on .compact-blank-row").length === 0')
            page.evaluate('''() => new Promise(resolve => findTerm(activeTerm).term.write('\\x1b[?1049l', resolve))''')
            page.wait_for_function('document.querySelectorAll(".term-host.on .compact-blank-row").length > 4')
            expect(rows.filter(has_text='Agent received: draft')).to_have_count(1)

        page.evaluate("AppAppearance.update({theme:'dark', fontSize:14}); changePanelWidth('terminal',480)")
        page.wait_for_function('findTerm(activeTerm).term.cols > 45')
        page.evaluate('findTerm(activeTerm).term.focus()')
        page.keyboard.type('after-resize')
        page.keyboard.press('Enter')
        expect(page.locator('.term-host.on .xterm-rows')).to_contain_text('Agent received: after-resize')
        terminal_id = page.evaluate('activeTerm')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('typeof terms !== "undefined" && terms.length === 2 && terms.every(t => t.ws.readyState === 1)')
        page.evaluate('id => focusTerm(id)', terminal_id)
        expect(page.locator('.term-host.on .xterm-rows')).to_contain_text('Agent received: after-resize')
        page.wait_for_function('document.querySelectorAll(".term-host.on .compact-blank-row").length > 4')
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-compact-agent.png'))
        # Scrollback retains every original line, while older messages also
        # render tightly and remain selectable at their displayed positions.
        page.evaluate('''() => new Promise(resolve => findTerm(activeTerm).term.write(
          '\\r\\n' + Array.from({length: 120}, (_, i) => `history${i}\\r\\n\\r\\n`).join(''), resolve))''')
        page.evaluate('findTerm(activeTerm).term.scrollToTop()')
        page.wait_for_function('findTerm(activeTerm).term.buffer.active.viewportY === 0')
        history = page.locator('.term-host.on .xterm-rows > div').filter(has_text='history0')
        expect(history).to_have_count(1)
        box = history.bounding_box()
        page.mouse.dblclick(box['x'] + 20, box['y'] + box['height'] / 2)
        assert page.evaluate('findTerm(activeTerm).term.getSelection()') == 'history0'
        page.evaluate('findTerm(activeTerm).term.selectAll()')
        transcript = page.evaluate('findTerm(activeTerm).term.getSelection()')
        assert 'history0\n\nhistory1' in transcript
        assert 'history119' in transcript
        page.evaluate('findTerm(activeTerm).term.clearSelection(); findTerm(activeTerm).term.scrollToBottom()')
        expect(page.locator('.term-host.on .xterm-rows')).to_contain_text('history119')
        page.evaluate('async () => { for (const t of [...terms]) await closeTerminal(t.id); }')
        assert not errors, errors
        browser.close()
        print('PASS: agent gaps removed; selection, input, alternate screen, resize, reload and scrollback remain correct')


if __name__ == '__main__':
    main()
