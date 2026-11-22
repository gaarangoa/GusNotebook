"""Normal terminal → HTML editor → linked Jupyter notebook, on a disposable server.

Uses the real terminal CLI and uv PCA without model credentials or model calls.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated
from gusnotebook.project_templates import PCA_CODE


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    work = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]).resolve() / "work"
    source = work / "measurements.csv"
    source.write_text("x,y,z\n1,8,3\n2,4,9\n3,7,5\n4,3,1\n5,2,8\n6,6,4\n")
    cli = str(Path(sys.executable).parent / "gusnb")
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("booted")
        expect(page.locator('#project-workspace')).to_have_count(0)
        page.evaluate("openTerminal(null, 'shell').then(t => t.id)")
        page.wait_for_function("activeTerm && findTerm(activeTerm)?.ws?.readyState === WebSocket.OPEN")
        terminal_id = page.evaluate('activeTerm')
        env = {**os.environ, "NB_TERMINAL": terminal_id, "NB_SESSION": page.evaluate('currentSession')}
        # Create through the real, already-open PTY, as the terminal agent does.
        command = shlex.join([cli, 'project', 'create', 'browser_study', str(work)])
        page.evaluate("cmd => findTerm(activeTerm).ws.send(cmd + '\\r')", command + ' > ' + shlex.quote(str(work / 'created.json')))
        page.wait_for_function("active?.endsWith('/browser_study/reports/deck.html')", timeout=15000)
        expect(page.locator('#html-preview-frame')).to_be_visible()
        expect(page.locator('#project-links')).to_be_visible()
        document = page.locator('.left').bounding_box()
        assert page.locator('#agent-pane').bounding_box()['x'] >= document['x'] + document['width']
        assert page.evaluate('activeTerm') == terminal_id
        expect(page.locator('#project-create-back')).not_to_be_visible()

        def command(*args, payload=None):
            result = subprocess.run([cli, 'project', *args], env=env,
                                    input=json.dumps(payload) if payload is not None else None,
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        def finish(kind):
            deadline = time.monotonic() + 180
            while True:
                status = command('status')['job']
                if status['kind'] == kind and status['status'] != 'running':
                    break
                assert time.monotonic() < deadline, status
                page.wait_for_timeout(250)
            assert status['status'] == 'completed', status
            return command('context')

        project = command('context')
        pid = project['id']
        assert page.evaluate('findTerm(activeTerm).project') == pid
        assert 'analysis.ipynb' in page.locator('#tabs').inner_text()
        command('source', 'measurements', str(source))
        command('prepare')
        finish('prepare')
        command('save', 'analysis', '-', payload={
            'revision': 'new', 'code': PCA_CODE, 'source_ids': ['measurements'],
            'parameters': {'source': 'measurements', 'columns': ['x', 'y', 'z']}})
        command('run', 'analysis')
        project = finish('run')
        run = project['runs'][-1]['id']
        command('publish', run)
        frame = page.frame_locator('#html-preview-frame')
        expect(frame.locator('svg circle')).to_have_count(6, timeout=15000)
        expect(frame.get_by_role('heading', name='Principal component analysis')).to_be_visible()
        expect(page.locator('#project-review')).to_have_text('Review 1 result')
        assert command('context')['analyses']['analysis']['accepted_run'] is None
        # This is the normal HTML editor with its source and visual controls.
        expect(page.locator('#text-editor')).not_to_be_visible()
        frame.locator('.analysis-link').click()
        page.wait_for_function("active?.includes('/results/runs/') && active.endsWith('/notebook.ipynb')")
        page.locator('#project-notebook').click()
        page.wait_for_function("active?.endsWith('/notebooks/analysis.ipynb')")
        notebook = json.loads((work / 'browser_study/notebooks/analysis.ipynb').read_text())
        assert 'def analyze(data, parameters):' in ''.join(notebook['cells'][1]['source'])
        assert notebook['metadata']['kernelspec']['notebook_python'] == project['environment']['python']
        assert notebook['cells'][1]['outputs'][0]['data']['application/json']['metrics']['rows_analyzed'] == 6
        # Human review stays in the workspace; the agent only published a draft.
        page.locator('#project-review').click()
        page.get_by_role('button', name='Accept result', exact=True).click()
        expect(page.locator('#project-reviews')).to_be_empty()
        assert command('context')['analyses']['analysis']['accepted_run'] == run
        command('note', 'Explained PCA exclusions in the terminal chat.')
        expect(page.locator('#project-history')).to_contain_text('Explained PCA exclusions', timeout=10000)
        page.locator('#project-history details').first.locator('summary').click()
        command('run', 'analysis', '--replay', run)
        replay = finish('replay')
        assert replay['runs'][-1]['comparison']['passed']
        expect(page.locator('#project-history details[open]')).to_have_count(1)
        page.get_by_role('button', name='Close project history').click()
        page.locator('#project-report').click()
        expect(frame.locator('svg circle')).to_have_count(6)
        expect(frame.get_by_role('heading', name='Principal component analysis')).to_be_visible()
        # Tab selection is persisted with the normal workspace's 100 ms debounce.
        page.wait_for_function('activeTimers.size === 0')
        page.wait_for_timeout(250)
        page.screenshot(path='/tmp/gusnb-normal-workspace.png')
        # Reattach the same terminal and restore the ordinary tabs on reload.
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function("booted && active?.endsWith('/reports/deck.html')")
        expect(page.locator('#project-links')).to_be_visible()
        page.wait_for_function("id => terms.some(t => t.id === id)", arg=terminal_id)
        expect(frame.locator('svg circle')).to_have_count(6)
        page.evaluate('AppAppearance.toggle()')
        expect(page.locator('#agent-pane .term-host.on')).to_be_visible()
        page.evaluate("openTerminalClipboard(findTerm(activeTerm), 'paste')")
        expect(page.get_by_label('Terminal text', exact=True)).to_be_visible()
        page.evaluate('closeTerminalClipboard(false)')
        # The optional + shortcut is a small setup dialog, not another workspace.
        page.locator('#tab-new').click()
        page.get_by_role('menuitem', name='Analysis project').click()
        expect(page.locator('#project-create-back')).to_be_visible()
        page.locator('#project-existing').select_option(pid)
        page.get_by_role('button', name='Open project', exact=True).click()
        expect(page.locator('#project-create-back')).not_to_be_visible()
        expect(page.locator('#html-preview-frame')).to_be_visible()
        assert not errors, errors
        browser.close()
    print('Normal workspace project workflow passed')


if __name__ == '__main__':
    main()
