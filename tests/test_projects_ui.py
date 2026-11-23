"""Normal terminal → HTML editor → linked Jupyter notebook, on a disposable server.

Uses the real terminal CLI, uv notebook kernel, D3 output and Git without model calls.
"""

import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import time

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated
from gusnotebook.plotting import D3_EXAMPLE
from gusnotebook.skills import code_of


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
        page.wait_for_function("active?.endsWith('/browser_study/results/reports/deck.html')", timeout=15000)
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
        frame = page.frame_locator('#html-preview-frame')
        expect(frame.locator('.slide')).to_have_count(5)
        expect(frame.get_by_role('heading', name='browser_study', exact=True)).to_be_visible()
        assert page.evaluate('findTerm(activeTerm).project') == pid
        assert 'analysis.ipynb' in page.locator('#tabs').inner_text()
        command('prepare')
        project = finish('prepare')
        root = Path(project['path'])
        notebook_path = root / 'code/notebooks/analysis.ipynb'
        code = code_of(D3_EXAMPLE).replace(
            'data = [{"label": "A", "value": 18}, {"label": "B", "value": 32}, {"label": "C", "value": 24}]',
            'import random\nrng = random.Random(7)\n# Explicitly requested synthetic example.\n'
            'data = [{"label": label, "value": rng.randint(10, 40)} for label in ["A", "B", "C"]]')
        nb_cli = [cli, '-n', str(notebook_path)]
        added = subprocess.run(nb_cli + ['add', '-'], env=env, input=code,
                               capture_output=True, text=True, timeout=60)
        assert added.returncode == 0, added.stderr
        cell_id = re.search(r'added code cell (\S+)', added.stdout).group(1)
        output = root / 'results/outputs/figure.html'
        exported = subprocess.run(nb_cli + ['export-output', cell_id, '--file', str(output)],
                                  env=env, capture_output=True, text=True, timeout=30)
        assert exported.returncode == 0, exported.stderr
        current = command('report')
        updated = work / 'updated.html'
        deck = re.sub(r'<div class="figure-slot">.*?</div>', lambda _: output.read_text(),
                      current['html'], count=1, flags=re.S)
        deck = deck.replace('Lead with the finding supported by the figure', 'Synthetic example')
        updated.write_text(deck)
        command('report', '--file', str(updated), '--revision', current['revision'])
        plot = frame.frame_locator('.gusnb-viz-frame')
        expect(plot.locator('svg .bar')).to_have_count(3, timeout=15000)
        expect(frame.get_by_role('heading', name='Synthetic example')).to_be_visible()
        expect(frame.locator('.figure-slot')).to_have_count(0)
        figure = frame.locator('.gusnb-viz').bounding_box()
        commentary = frame.locator('.figure-text aside').bounding_box()
        assert figure['x'] + figure['width'] <= commentary['x'] + 1
        expect(page.locator('#project-review')).to_have_count(0)
        expect(page.locator('#project-history-back')).to_have_count(0)
        assert 'runs' not in command('context')
        assert not (root / 'results/runs').exists()
        assert not (root / 'results/reports/views').exists()
        assert not (root / 'data/.gusnotebook/audit/artifacts').exists()
        expect(page.locator('#text-editor')).not_to_be_visible()
        frame.get_by_role('button', name='Notebook code', exact=True).click()
        page.wait_for_function('p => active === p', arg=str(notebook_path))
        notebook = json.loads(notebook_path.read_text())
        assert 'random.Random(7)' in ''.join(notebook['cells'][1]['source'])
        assert 'd3.select' in ''.join(notebook['cells'][1]['source'])
        assert notebook['metadata']['kernelspec']['notebook_python'] == project['environment']['python']
        assert notebook['cells'][1]['outputs'][0]['data']['text/html']

        # The existing Git panel versions source and notebooks, with no run-ID UI.
        page.locator('#project-git').click()
        page.wait_for_function('p => gitState?.root === p', arg=str(root))
        assert (root / '.git').is_dir()
        for key, value in [('user.name', 'Notebook Test'), ('user.email', 'notebook-test@example.invalid')]:
            subprocess.run(['git', '-C', str(root), 'config', key, value], check=True)
        (root / 'code/tests/test_example.py').write_text('def test_example(): pass\n')
        tracked = ['.gitignore', 'code']
        page.evaluate('files => gitAction("stage", files)', tracked)
        page.wait_for_function('gitState?.operation?.action === "stage" && gitState.operation.state === "done"')
        page.locator('#git-message').fill('Add synthetic D3 figure with notebook source')
        page.locator('#git-commit-button').click()
        page.wait_for_function('gitState?.operation?.action === "commit" && gitState.operation.state === "done"')
        listing = subprocess.check_output(['git', '-C', str(root), 'ls-files'], text=True).splitlines()
        assert 'code/notebooks/analysis.ipynb' in listing and 'code/uv.lock' in listing, listing
        assert all(name == '.gitignore' or name.startswith('code/') for name in listing), listing
        assert 'code/tests/test_example.py' not in listing, listing
        assert not any(name.startswith(('data/', 'results/', 'environment/', 'audit/')) for name in listing), listing
        committed = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:code/notebooks/analysis.ipynb'], text=True)
        assert 'd3.select' in committed and 'gusnotebook_history' in committed
        page.locator('#project-report').click()
        expect(plot.locator('svg .bar')).to_have_count(3)
        # Tab selection is persisted with the normal workspace's 100 ms debounce.
        page.wait_for_function('activeTimers.size === 0')
        page.wait_for_timeout(250)
        page.screenshot(path='/tmp/gusnb-normal-workspace.png')
        # Reattach the same terminal and restore the ordinary tabs on reload.
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function("booted && active?.endsWith('/reports/deck.html')")
        expect(page.locator('#project-links')).to_be_visible()
        page.wait_for_function("id => terms.some(t => t.id === id)", arg=terminal_id)
        expect(plot.locator('svg .bar')).to_have_count(3)
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
