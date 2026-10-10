"""Local Mermaid rendering, source round trips, themes, errors and isolation."""

import os
from pathlib import Path
import tempfile
from urllib.parse import unquote

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    work = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work'
    document = work / 'diagrams # α.md'
    diagrams = [
        'flowchart LR\n    A[Start] --> B{Ready?}\n    B -->|Yes| C[Finish]\n',
        'sequenceDiagram\n    Alice->>Bob: Hello\n    Bob-->>Alice: Hi\n',
        'stateDiagram-v2\n    [*] --> Ready\n    Ready --> Done\n    Done --> [*]\n',
    ]
    original = '# Diagrams\n\nInline $x^2$ stays rendered.\n\n' + '\n'.join(
        '```mermaid\n' + diagram + '```\n' for diagram in diagrams) + '\n```python\nvalue = 42\n```\n'
    original += '\n```mermaid\nnot a valid diagram\n```\n'
    original += '\n<div class="markdown-mermaid" data-mermaid-source="flowchart LR; X-->Y">Raw HTML</div>\n'
    document.write_text(original)
    alternate = work / 'other.markdown'
    alternate.write_text('# Other\n\n```mermaid\nflowchart LR\n    X[Other diagram] --> Y[Next]\n```\n')
    screenshots = Path(tempfile.gettempdir()) / 'gusnb-mermaid'
    screenshots.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width': 1440, 'height': 1080}, permissions=['clipboard-read', 'clipboard-write'])
        authenticate_browser(page.context, url)
        errors, network_errors, external, console_errors = [], [], [], []
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: network_errors.append(response.url)
                if '/static/' in response.url and response.status >= 400 else None)
        page.on('request', lambda request: external.append(request.url)
                if request.url.startswith(('http://', 'https://')) and not request.url.startswith(url.rstrip('/')) else None)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_function('booted')
        # The renderer must not load until a document actually uses Mermaid.
        assert not page.locator('.mermaid-renderer').count()
        page.evaluate('path => openFile(path)', str(document))
        blocks = page.locator('#markdown-preview .markdown-mermaid')
        expect(blocks).to_have_count(4)
        try:
            expect(page.locator('#markdown-preview .mermaid-diagram img')).to_have_count(3, timeout=30000)
        except AssertionError:
            print({'errors': errors, 'console': console_errors,
                   'status': page.locator('.mermaid-status').all_text_contents()}, flush=True)
            raise
        expect(blocks.nth(3).locator('.mermaid-status')).to_contain_text('Could not render')
        expect(blocks.nth(3).locator('details')).to_have_attribute('open', '')
        expect(page.locator('#markdown-preview .katex')).to_have_count(1)
        expect(page.locator('#markdown-preview .hljs-number')).to_have_text('42')
        for index, text in enumerate(('Start', 'Alice', 'Ready')):
            source = blocks.nth(index).locator('.mermaid-diagram img').get_attribute('src')
            assert text in unquote(source.split(',', 1)[1])
            assert '<foreignObject' not in unquote(source.split(',', 1)[1])
        blocks.first.locator('summary').click()
        blocks.first.locator('.markdown-code-copy').click()
        expect(blocks.first.locator('.markdown-code-copy')).to_have_text('Copied')
        assert page.evaluate('navigator.clipboard.readText()') == diagrams[0]
        assert document.read_text() == original
        assert page.locator('.mermaid-renderer').get_attribute('sandbox') == 'allow-scripts'
        print('PASS: flowchart, sequence, state, code/math compatibility, error fallback, exact source copy and local isolated renderer', flush=True)

        for theme in ('dark', 'light'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            page.wait_for_function('''theme => [...document.querySelectorAll('#markdown-preview .mermaid-diagram img')]
              .length === 3 && [...document.querySelectorAll('#markdown-preview .mermaid-diagram img')]
              .every(image => image.dataset.theme === theme)''', arg=theme)
            page.screenshot(path=str(screenshots / f'{theme}.png'))
        for style in ('reading', 'paper', 'compact'):
            page.evaluate('style => AppAppearance.update({markdownStyle: style})', style)
            page.wait_for_function('''() => [...document.querySelectorAll('#markdown-preview .mermaid-diagram')]
              .every(block => block.getAttribute('aria-busy') === 'false')''')
        for width in (768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 900})
            page.wait_for_timeout(200)
            assert page.locator('#markdown-preview').evaluate('el => el.scrollWidth <= el.clientWidth')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 1080})
        page.click('#markdown-source-button')
        expect(page.locator('#text-editor')).to_have_value(original)
        edited = '# Updated\n\n```mermaid\nflowchart LR\n    New[New graph] --> Saved[Saved graph]\n```\n'
        page.locator('#text-editor').fill(edited)
        page.click('#markdown-preview-button')
        expect(page.locator('#markdown-preview .mermaid-diagram img')).to_have_count(1)
        updated_svg = unquote(page.locator('#markdown-preview .mermaid-diagram img').get_attribute('src'))
        assert 'New' in updated_svg and 'Saved' in updated_svg, updated_svg[-2000:]
        assert document.read_text() == original
        page.click('#text-save')
        page.wait_for_function('!activeTab().dirty && !activeTab().saveInFlight')
        assert document.read_text() == edited
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('booted')
        expect(page.locator('#markdown-preview .mermaid-diagram img')).to_have_count(1, timeout=30000)
        print('PASS: light/dark, all Markdown styles, narrow layouts, unsaved previews, save and reload', flush=True)

        # A diagram cannot execute markup, callbacks or external resource loads,
        # even when it tries to override the configured renderer settings.
        hostile = '''# Hostile diagram
```mermaid
%%{init: {"securityLevel":"loose","htmlLabels":true,"flowchart":{"htmlLabels":true}}}%%
flowchart LR
    A["<img src='https://example.invalid/probe' onerror='parent.mermaidXss=true'>"] --> B[Safe]
    click B "javascript:parent.mermaidXss=true"
```
'''
        page.click('#markdown-source-button')
        page.locator('#text-editor').fill(hostile)
        page.click('#markdown-preview-button')
        page.wait_for_function('document.querySelector(".mermaid-diagram").getAttribute("aria-busy") === "false"')
        assert not page.evaluate('!!window.mermaidXss')
        assert 'https://example.invalid/probe' not in external
        expect(page.locator('#markdown-preview .mermaid-diagram :is(svg,script,a,iframe)')).to_have_count(0)
        # Size failures preserve source and don't block later valid diagrams.
        page.click('#markdown-source-button')
        page.locator('#text-editor').fill('```mermaid\nflowchart LR\n%% ' + 'x' * 50001 + '\n```\n\n```mermaid\nflowchart LR\nA-->B\n```\n')
        page.click('#markdown-preview-button')
        expect(page.locator('.mermaid-status').first).to_contain_text('50,000 characters')
        expect(page.locator('#markdown-preview .mermaid-diagram img')).to_have_count(1)
        # Changing tabs while jobs are pending cannot paint an older document.
        page.evaluate('path => openFile(path)', str(alternate))
        expect(page.locator('#markdown-preview .mermaid-diagram img')).to_have_count(1)
        other_svg = unquote(page.locator('#markdown-preview .mermaid-diagram img').get_attribute('src'))
        assert 'Other' in other_svg and 'Next' in other_svg
        assert not external, external
        assert not network_errors, network_errors
        assert not errors, errors
        print('PASS: markup/directive isolation, source-size fallback, subsequent recovery, tab changes and no external requests or browser errors', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
