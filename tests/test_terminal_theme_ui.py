"""Terminal colors and live input across theme switches, using a disposable PTY."""

import os
from pathlib import Path
import shlex
import sys
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


# Reproduce an agent TUI that keeps explicit ANSI/RGB colors after the host
# changes theme. No agent credentials, model calls, or user terminals are used.
PROMPT = r'''
import os, sys, termios, tty
fd = sys.stdin.fileno()
saved = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    sys.stdout.write('\x1b[0m\x1b[2J\x1b[H'
        '\x1b[39;49mDefault text\r\n'
        '\x1b[39;48;2;32;36;45mFixed dark background\x1b[0m\r\n'
        '\x1b[38;2;231;234;240mFixed light text\x1b[0m\r\n'
        '\x1b[38;2;32;38;50mFixed dark text\x1b[0m\r\n'
        '\x1b[38;5;252mIndexed light text\x1b[0m\r\n'
        '\x1b[37;40mWhite on black\x1b[0m\r\n'
        '\x1b[30;47mBlack on white\x1b[0m\r\n'
        '\x1b[39;48;2;32;36;45mInput: ')
    sys.stdout.flush()
    while True:
        data = os.read(fd, 1)
        if not data or data == b'\x03':
            break
        os.write(sys.stdout.fileno(), data)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
'''


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    prompt = work / 'colored_prompt.py'
    prompt.write_text(PROMPT)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        authenticate_browser(page.context, url)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.evaluate('AppAppearance.update({theme: "dark"})')
        page.evaluate('path => openTerminal(path, "shell")', str(work))
        page.wait_for_function('terms.length === 1 && terms[0].ws.readyState === 1')
        page.evaluate('window.originalTerm = terms[0].term; window.originalSocket = terms[0].ws')
        command = f'{shlex.quote(sys.executable)} -u {shlex.quote(str(prompt))}\r'
        page.evaluate('command => terms[0].ws.send(command)', command)
        rows = page.locator('.term-host.on .xterm-rows')
        expect(rows).to_contain_text('Input: ')

        def expect_readable():
            # Inspect the renderer's actual colors, including retained RGB and
            # 256-color cells, rather than just checking the theme option.
            check = r'''() => {
              const luminance = css => {
                const rgb = css.match(/[\d.]+/g).slice(0, 3).map(Number).map(c => {
                  c /= 255; return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
                });
                return .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
              };
              const host = document.querySelector('.term-host.on');
              const fallback = getComputedStyle(host.querySelector('.xterm-viewport')).backgroundColor;
              const spans = [...host.querySelectorAll('.xterm-rows span')].filter(el => el.textContent.trim());
              window.terminalContrastFailures = spans.map(el => {
                const style = getComputedStyle(el);
                const bg = style.backgroundColor === 'rgba(0, 0, 0, 0)' ? fallback : style.backgroundColor;
                const a = luminance(style.color), b = luminance(bg);
                return {text: el.textContent, color: style.color, background: bg,
                  ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05)};
              }).filter(cell => cell.ratio < 4.5);
              return spans.length && !terminalContrastFailures.length;
            }'''
            try:
                page.wait_for_function(check, timeout=5000)
            except PlaywrightTimeoutError:
                raise AssertionError(page.evaluate('terminalContrastFailures')) from None

        # Use the app's theme control, with a draft still in the running prompt.
        page.keyboard.type('before')
        expect(rows).to_contain_text('Input: before')
        for theme in ['light', 'dark', 'light']:
            page.click('#workspace-more')
            page.click('#theme-toggle')
            expect(page.locator('html')).to_have_attribute('data-theme', theme)
            expect(page.locator('.term-host.on .xterm-viewport')).to_have_css(
                'background-color', 'rgb(255, 255, 255)' if theme == 'light' else 'rgb(24, 27, 34)')
            expect_readable()
            page.evaluate('terms[0].term.focus()')
            page.keyboard.type('-' + theme)
            expect(rows).to_contain_text('before-light' if theme == 'light' else 'before-light-dark')
            expect_readable()
            assert page.evaluate('terms[0].term === originalTerm && terms[0].ws === originalSocket && originalSocket.readyState === 1')
        expect(rows).to_contain_text('Input: before-light-dark-light')

        # Inactive terminals receive the same update and retain their draft.
        page.evaluate('path => openTerminal(path, "shell")', str(work))
        page.wait_for_function('terms.length === 2 && terms[1].ws.readyState === 1')
        page.click('#workspace-more')
        page.click('#theme-toggle')
        page.evaluate('focusTerm(terms[0].id)')
        expect(rows).to_contain_text('Input: before-light-dark-light')
        expect_readable()
        # Reattaching replays the same colored output into a fresh renderer.
        terminal_id = page.evaluate('terms[0].id')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('typeof terms !== "undefined" && terms.length === 2 && terms.every(t => t.ws.readyState === 1)')
        page.evaluate('id => focusTerm(id)', terminal_id)
        expect(rows).to_contain_text('Input: before-light-dark-light')
        page.click('#workspace-more')
        page.click('#theme-toggle')
        expect_readable()
        screenshot = Path(tempfile.gettempdir()) / 'gusnb-terminal-theme.png'
        page.screenshot(path=str(screenshot))
        assert not errors, errors
        print('PASS: readable ANSI/RGB terminal colors and typed drafts across themes, inactive tabs, and reload', flush=True)
        print('Screenshot: ' + str(screenshot), flush=True)
        browser.close()


if __name__ == '__main__':
    main()
