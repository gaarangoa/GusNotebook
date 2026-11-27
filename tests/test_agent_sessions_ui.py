"""Folder history browsing and native resume, using fake CLIs and real PTYs."""

import os
import json
import shutil
import sys
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated
from test_agent_sessions import write_session


def main():
    if os.environ.get('GUSNOTEBOOK_ISOLATED_TEST') != '1':
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'bin'
            binary.mkdir()
            (binary / 'uv').symlink_to(shutil.which('uv'))
            for name in ('claude', 'codex'):
                path = binary / name
                path.write_text('#!/bin/sh\nprintf "Saved session test terminal\\n"\nfor arg do\n case "$arg" in --resume|resume|????????-????-????-????-????????????) printf "%s\\n" "$arg";; esac\ndone\nexec /bin/cat\n')
                path.chmod(0o755)
            # Emulate only the native metadata protocol, never model calls.
            (binary/'codex').write_text('#!'+sys.executable+'\n'+'''import json, os, sys
from pathlib import Path
if sys.argv[1:2] == ['app-server']:
    root = Path(os.environ['CODEX_HOME'])
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request: continue
        params = request.get('params', {})
        if request['method'] == 'thread/name/set':
            with (root/'session_index.jsonl').open('a') as stream:
                stream.write(json.dumps({'id':params['threadId'], 'thread_name':params['name']})+'\\n')
        elif request['method'] == 'thread/delete':
            for path in (root/'sessions').glob('*/*/*/*'+params['threadId']+'.jsonl'):
                path.unlink()
        print(json.dumps({'id':request['id'], 'result':{}}), flush=True)
else:
    print('Saved session test terminal', flush=True)
    print(sys.argv[-1], flush=True)
    os.execv('/bin/cat', ['/bin/cat'])
''')
            os.environ.update(CODEX_HOME=str(root/'codex'), CLAUDE_CONFIG_DIR=str(root/'claude'),
                              GUSNOTEBOOK_UV=str(binary/'uv'),
                              PATH=str(binary) + os.pathsep + os.environ['PATH'])
            rerun_isolated(__file__)
    work = (Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work').resolve()
    codex, claude = Path(os.environ['CODEX_HOME']), Path(os.environ['CLAUDE_CONFIG_DIR'])
    nested = work / 'nested'
    nested.mkdir()
    outside = work.parent / 'unrelated'
    outside.mkdir()
    codex_id, codex_path = write_session(codex, 'codex', work, 'PCA comparison')
    claude_id, claude_path = write_session(claude, 'claude', work, 'Deck discussion <img src=x onerror=window.__injected=1>')
    write_session(codex, 'codex', nested, 'Nested analysis')
    write_session(claude, 'claude', outside, 'Unrelated session')
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={'width':1440, 'height':960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        authenticate_browser(page.context, url)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.evaluate('path => browse(path)', str(work))
        button = page.get_by_role('button', name='Agent sessions in this folder', exact=True)
        button.click()
        dialog = page.get_by_role('dialog', name='Agent sessions', exact=True)
        expect(dialog).to_be_visible()
        expect(page.locator('#agent-history-folder')).to_have_text(str(work))
        expect(page.locator('.agent-history-row')).to_have_count(2)
        expect(page.locator('#agent-history-list')).not_to_contain_text('Unrelated session')
        assert page.evaluate('window.__injected') is None
        page.locator('.agent-history-view').filter(has_text='Deck discussion').click()
        expect(page.locator('#agent-history-messages')).to_contain_text('Here are the results.')
        expect(page.locator('#agent-history-conversation-meta')).to_contain_text(claude_id)
        page.get_by_role('button', name='← All sessions', exact=True).click()
        page.locator('#agent-history-children').check()
        expect(page.locator('.agent-history-row')).to_have_count(3)
        page.locator('#agent-history-provider').select_option('codex')
        expect(page.locator('.agent-history-row')).to_have_count(2)
        page.locator('#agent-history-query').fill('PCA')
        expect(page.locator('.agent-history-row')).to_have_count(1)
        for theme in ('dark', 'light'):
            page.evaluate('theme => AppAppearance.update({theme})', theme)
            expect(dialog).to_be_visible()
        page.screenshot(path=str(Path(tempfile.gettempdir())/'gusnb-agent-sessions.png'))
        page.locator('.agent-history-view').click()
        page.route('**/api/terminals*', lambda route: route.fulfill(status=400, json={'error':'Resume test failure'}))
        page.get_by_role('button', name='Resume in terminal', exact=True).click()
        expect(page.locator('#agent-history-error')).to_contain_text('Resume test failure')
        expect(dialog).to_be_visible()
        page.unroute('**/api/terminals*')
        page.get_by_role('button', name='Resume in terminal', exact=True).click()
        expect(dialog).to_be_hidden()
        page.wait_for_function('terms.length === 1 && terms[0].ws.readyState === 1')
        expect(page.locator('.term-host.on')).to_contain_text('Saved session test terminal')
        expect(page.locator('.term-host.on')).to_contain_text(codex_id)
        assert page.evaluate('terms[0].kind') == 'codex'
        assert page.evaluate('terms[0].project') is None
        first = page.evaluate('terms[0].id')
        button.click()
        expect(page.locator('.agent-history-row')).to_have_count(1)
        page.locator('.agent-history-row').get_by_role('button', name='Resume', exact=True).click()
        expect(dialog).to_be_hidden()
        assert page.evaluate('terms.length') == 1
        assert page.evaluate('activeTerm') == first
        # The native session reference survives browser reattachment.
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('terms.length === 1 && terms[0].ws.readyState === 1')
        assert page.evaluate('terms[0].agent_session') == codex_id
        button.click()
        expect(page.locator('.agent-history-row')).to_have_count(2)
        page.keyboard.press('Escape')
        expect(dialog).to_be_hidden()
        expect(button).to_be_focused()
        page.evaluate('path => browse(path)', str(outside))
        button.click()
        expect(page.locator('.agent-history-row')).to_have_count(1)
        expect(page.locator('#agent-history-list')).to_contain_text('Unrelated session')
        page.keyboard.press('Escape')
        page.evaluate('path => browse(path)', str(work))
        button.click()
        claude_row = page.locator('.agent-history-row').filter(has_text='Deck discussion')
        claude_row.get_by_role('button', name='Rename session', exact=True).click()
        expect(page.locator('#ask-back')).to_be_visible()
        page.locator('#ask-input').fill('Claude analysis notes')
        page.locator('#ask-ok').click()
        expect(page.locator('#agent-history-list')).to_contain_text('Claude analysis notes')
        assert json.loads(claude_path.read_text().splitlines()[-1])['customTitle'] == 'Claude analysis notes'

        codex_row = page.locator('.agent-history-row').filter(has_text='PCA comparison')
        codex_row.locator('.agent-history-view').click()
        page.get_by_role('button', name='Rename', exact=True).click()
        page.locator('#ask-input').fill('PCA result notes')
        page.locator('#ask-ok').click()
        expect(page.locator('#agent-history-conversation-title')).to_have_text('PCA result notes')
        expect(page.locator('#term-tabs .tn')).to_have_text('PCA result notes')
        expect(page.locator('#agent-history-delete')).to_be_disabled()
        page.get_by_role('button', name='← All sessions', exact=True).click()
        expect(page.locator('#agent-history-list')).to_contain_text('PCA result notes')

        claude_row = page.locator('.agent-history-row').filter(has_text='Claude analysis notes')
        claude_row.get_by_role('button', name='Delete session', exact=True).click()
        page.locator('#ask-back').get_by_role('button', name='Cancel', exact=True).click()
        assert claude_path.exists()
        claude_row.get_by_role('button', name='Delete session', exact=True).click()
        page.locator('#ask-ok').click()
        expect(page.locator('.agent-history-row')).to_have_count(1)
        assert not claude_path.exists()
        assert codex_path.exists()

        # A rename survives reattachment and resumes the same native ID.
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('terms.length === 1 && terms[0].ws.readyState === 1')
        assert page.evaluate('terms[0].label') == 'PCA result notes'
        button.click()
        page.locator('.agent-history-row').get_by_role('button', name='Resume', exact=True).click()
        expect(dialog).to_be_hidden()
        assert page.evaluate('terms.length') == 1
        assert page.evaluate('terms[0].agent_session') == codex_id
        page.evaluate('async () => { await api("/api/terminals/" + terms[0].id, {method:"DELETE"}); }')
        button.click()
        codex_row = page.locator('.agent-history-row').filter(has_text='PCA result notes')
        expect(codex_row.get_by_role('button', name='Delete session', exact=True)).to_be_enabled()
        codex_row.get_by_role('button', name='Delete session', exact=True).click()
        page.locator('#ask-ok').click()
        expect(page.locator('.agent-history-row')).to_have_count(0)
        assert not codex_path.exists()
        assert (work/'notebook.ipynb').exists()
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        button.click()
        expect(page.locator('#agent-history-status')).to_have_text('0 saved sessions')
        assert not errors, errors
        browser.close()
        print('PASS: folder catalog, previews, native rename/delete, confirmation, live-session protection, resume, and persistence')


if __name__ == '__main__':
    main()
