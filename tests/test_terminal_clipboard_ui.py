"""Real clipboard gestures reach the chosen PTY once, with safe fallbacks."""

import os
from pathlib import Path
import sys
import tempfile
import time

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


# Receives bytes without executing clipboard contents. Shell and both agent
# adapters use real PTYs and WebSockets, with no agent account involved.
RECEIVER = r'''
import os, sys, tty
from pathlib import Path
tty.setraw(0)
path = Path.cwd() / (Path(sys.argv[0]).name + '-input')
with path.open('ab', buffering=0) as received:
    os.write(1, b'\x1b[?2004h\x1b[2J\x1b[HReady\r\n\r\nCLIPBOARDWORD\r\n\r\nInput: ')
    while True:
        data = os.read(0, 8192)
        if not data: break
        received.write(data)
'''


def eventually(check):
    deadline = time.monotonic() + 5
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError('Clipboard operation did not complete')
        time.sleep(.05)


def main():
    if os.environ.get('GUSNOTEBOOK_ISOLATED_TEST') != '1':
        with tempfile.TemporaryDirectory(prefix='gusnb-clipboard-') as directory:
            from gusnotebook.environments import uv_binary
            uv = uv_binary()
            if uv:
                link = Path(directory) / 'uv'
                link.symlink_to(uv)
                os.environ['GUSNOTEBOOK_UV'] = str(link)
            for name in ('clipboard-shell', 'claude', 'codex'):
                command = Path(directory) / name
                command.write_text('#!' + sys.executable + '\n' + RECEIVER)
                command.chmod(0o755)
            os.environ['SHELL'] = str(Path(directory) / 'clipboard-shell')
            os.environ['PATH'] = directory + os.pathsep + os.environ['PATH']
            rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960},
                                permissions=['clipboard-read', 'clipboard-write'])
        authenticate_browser(page.context, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        page.evaluate('window.testClipboard = navigator.clipboard')
        native_paste = 'Meta+v' if page.evaluate('terminalClipboardMac') else 'Control+v'

        def select_word():
            row = page.locator('.term-host.on .xterm-rows > div').filter(has_text='CLIPBOARDWORD')
            box = row.bounding_box()
            page.mouse.dblclick(box['x'] + 15, box['y'] + box['height'] / 2)
            assert page.evaluate('findTerm(activeTerm).term.getSelection()') == 'CLIPBOARDWORD'

        def menu(action):
            row = page.locator('.term-host.on .xterm-rows > div').filter(has_text='CLIPBOARDWORD')
            row.click(button='right')
            page.get_by_role('menu', name='Terminal actions').get_by_role('menuitem', name=action, exact=True).click()

        def copied():
            eventually(lambda: page.evaluate('testClipboard.readText()') == 'CLIPBOARDWORD')

        for kind in ('shell', 'claude', 'codex'):
            page.evaluate('kind => openTerminal(null, kind)', kind)
            page.wait_for_function('findTerm(activeTerm)?.ws.readyState === 1')
            expect(page.locator('.term-host.on .xterm-rows')).to_contain_text('CLIPBOARDWORD')
            if kind != 'shell':
                page.wait_for_selector('.term-host.on .compact-agent-rows')
            received = work / (('clipboard-shell' if kind == 'shell' else kind) + '-input')
            expected = b''
            for shortcut in (['Meta+c', 'Control+Shift+c'] if page.evaluate('terminalClipboardMac') else ['Control+Shift+c']):
                select_word()
                page.evaluate("testClipboard.writeText('before')")
                page.keyboard.press(shortcut)
                copied()
                assert received.read_bytes() == expected
            select_word()
            menu('Copy')
            copied()
            for shortcut in (native_paste, 'Control+Shift+v', None):
                text = 'α first line\nsecond line'
                page.evaluate('text => testClipboard.writeText(text)', text)
                if shortcut:
                    page.evaluate('findTerm(activeTerm).term.focus()')
                    page.keyboard.press(shortcut)
                else:
                    menu('Paste')
                expected += ('\x1b[200~' + text.replace('\n', '\r') + '\x1b[201~').encode()
                eventually(lambda: received.read_bytes() == expected)
            # Interrupt remains available even while output is selected.
            select_word()
            page.keyboard.press('Control+c')
            expected += b'\x03'
            eventually(lambda: received.read_bytes() == expected)
            print(f'PASS: {kind} copy/paste shortcuts, right-click actions, Unicode, bracketed paste and Ctrl+C', flush=True)

        # Remote HTTP and denied API access both retain native clipboard paths.
        for unavailable in ('missing', 'denied'):
            page.evaluate('''mode => Object.defineProperty(navigator, 'clipboard', {
              configurable:true, value:mode === 'missing' ? undefined : {
                readText:async () => {throw new Error('denied')},
                writeText:async () => {throw new Error('denied')}
              }
            })''', unavailable)
            select_word()
            page.evaluate("testClipboard.writeText('before fallback')")
            menu('Copy')
            copied()
            page.evaluate("testClipboard.writeText('native paste')")
            page.evaluate('findTerm(activeTerm).term.focus()')
            page.keyboard.press(native_paste)
            expected += b'\x1b[200~native paste\x1b[201~'
            eventually(lambda: received.read_bytes() == expected)
            page.keyboard.press('Control+Shift+v')
            dialog = page.get_by_role('dialog', name='Paste into terminal')
            expect(dialog).to_be_visible()
            page.evaluate('text => testClipboard.writeText(text)', 'reviewed\nmultiline')
            page.get_by_label('Terminal text', exact=True).focus()
            page.keyboard.press(native_paste)
            expect(page.get_by_label('Terminal text', exact=True)).to_have_value('reviewed\nmultiline')
            assert received.read_bytes() == expected
            dialog.get_by_role('button', name='Paste', exact=True).click()
            expected += b'\x1b[200~reviewed\rmultiline\x1b[201~'
            eventually(lambda: received.read_bytes() == expected)
            expect(dialog).not_to_be_visible()
            page.keyboard.press('Control+Shift+v')
            expect(dialog).to_be_visible()
            page.get_by_label('Terminal text', exact=True).fill('canceled')
            page.keyboard.press('Escape')
            expect(dialog).not_to_be_visible()
            assert page.evaluate('document.activeElement === findTerm(activeTerm).term.textarea')
            assert page.locator('#terminal-clipboard-text').input_value() == ''
            assert received.read_bytes() == expected
            print(f'PASS: {unavailable} clipboard API uses native copy/paste and a cancellable multiline paste dialog', flush=True)

        # If even native programmatic copy is blocked, keep the text selectable.
        select_word()
        page.evaluate('() => { window.originalCopyCommand = document.execCommand; document.execCommand = () => false; }')
        menu('Copy')
        dialog = page.get_by_role('dialog', name='Copy terminal selection')
        expect(dialog).to_be_visible()
        expect(page.get_by_label('Terminal text', exact=True)).to_have_value('CLIPBOARDWORD')
        page.keyboard.press('Meta+c' if page.evaluate('terminalClipboardMac') else 'Control+c')
        copied()
        dialog.get_by_role('button', name='Done', exact=True).click()
        expect(dialog).not_to_be_visible()
        page.evaluate('() => { document.execCommand = window.originalCopyCommand; }')
        assert received.read_bytes() == expected

        # A slow permission response must not paste into a different tab.
        page.evaluate('''() => {
          Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
            readText:() => new Promise(resolve => {window.finishClipboardRead=resolve})
          }});
        }''')
        page.keyboard.press('Control+Shift+v')
        page.wait_for_function('typeof finishClipboardRead === "function"')
        other_received = (work / 'clipboard-shell-input').read_bytes()
        page.evaluate('focusTerm(terms[0].id); finishClipboardRead("wrong terminal")')
        page.wait_for_function('!terms.at(-1).clipboardReadPending')
        assert received.read_bytes() == expected
        assert (work / 'clipboard-shell-input').read_bytes() == other_received
        page.evaluate('delete navigator.clipboard')
        assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
