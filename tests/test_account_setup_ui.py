"""One-click setup: progress, cancel, retry, both sign-ins and app restart."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from gusnotebook.app import create_app, close_app
from gusnotebook.git_process import GitError
from gusnotebook.tool_installer import target
from test_tool_installer import archive
from ui_server import authenticate_browser, launch_browser


class QuietHandler(WSGIRequestHandler):
    def log(self, *_args, **_kwargs):
        pass


@contextmanager
def server(root):
    app = create_app({"WORK_DIR": root / 'work', "STATE_DIR": root / 'state',
                      "START_WATCHERS": False, "AUTH_TOKEN": 'setup-test-token'})
    http = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, f'http://localhost:{http.server_port}/'
    finally:
        http.shutdown()
        thread.join(3)
        close_app(app)
        http.server_close()


def main():
    with tempfile.TemporaryDirectory(prefix='gusnb-setup-ui-') as temporary:
        root = Path(temporary).resolve()
        for name in ['work', 'github', 'tunnels', 'home']:
            (root / name).mkdir()
        (root / 'tunnels/account.json').write_text('{"status":"Logged out"}')
        (root / 'tunnels/device-login').touch()
        system, arch = target()
        kind = 'zip' if system == 'macOS' else 'tar'
        member = f'gh_1.2.3_{system}_{arch}/bin/gh'
        gh = archive(('#!' + sys.executable + '\n' + Path(__file__).with_name('fake_gh.py').read_text()).encode(), member, kind)
        dev_payload = ('#!' + sys.executable + '\n' + Path(__file__).with_name('fake_devtunnel.py').read_text()).encode()
        dev = archive(dev_payload, 'devtunnel') if system == 'macOS' else dev_payload
        release = json.dumps({'tag_name':'v1.2.3','assets':[{
            'name':f'gh_1.2.3_{system}_{arch}.{"zip" if system == "macOS" else "tar.gz"}',
            'digest':'sha256:' + hashlib.sha256(gh).hexdigest()}]}).encode()
        attempts = []
        def download(installer, url, destination, cancel, progress, **kwargs):
            if 'api.github.com' in url:
                destination.write_bytes(release)
                return
            if '/cli/cli/' in url:
                attempts.append(url)
                progress(state='downloading', received=25, total=100)
                if len(attempts) == 1:
                    while not cancel.wait(.05):
                        pass
                    raise GitError('Setup canceled')
                if len(attempts) == 2:
                    raise GitError('Test download failed. Retry setup.')
                payload = gh
            else:
                payload = dev
            destination.write_bytes(payload)
            progress(state='downloading', received=len(payload), total=len(payload))

        original_which = shutil.which
        def which(name, *args, **kwargs):
            return None if name in {'gh', 'devtunnel'} else original_which(name, *args, **kwargs)
        environment = {'GUSNOTEBOOK_GH':'', 'GUSNOTEBOOK_DEVTUNNEL':'',
            'GUSNOTEBOOK_FAKE_GH':str(root / 'github'), 'GUSNOTEBOOK_FAKE_TUNNEL':str(root / 'tunnels'),
            'GUSNOTEBOOK_FAKE_TUNNEL_USERNAME':'researcher', 'GH_TOKEN':'', 'GITHUB_TOKEN':'',
            'NB_TOKEN':'setup-test-token', 'NO_LLM':'1'}
        with patch.dict(os.environ, environment), patch('shutil.which', side_effect=which), \
             patch('pathlib.Path.home', return_value=root / 'home'), \
             patch('gusnotebook.tool_installer.ToolInstaller._download', new=download), sync_playwright() as playwright:
            browser = launch_browser(playwright)
            context = browser.new_context(viewport={'width':1440, 'height':900})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with server(root) as (app, url):
                authenticate_browser(context, url)
                page.goto(url)
                page.locator('#accounts-button').click()
                page.get_by_role('button', name='Set up GitHub', exact=True).click()
                expect(page.locator('#account-tool-progress')).to_contain_text('GitHub CLI · Downloading (25%)')
                page.reload()
                page.locator('#accounts-button').click()
                expect(page.locator('#account-tool-progress')).to_contain_text('Downloading (25%)')
                page.get_by_role('button', name='Cancel setup', exact=True).click()
                expect(page.locator('#account-activity')).to_have_text('Setup canceled')
                assert list((root / 'state/tools').iterdir()) == []
                page.get_by_role('button', name='Set up GitHub', exact=True).click()
                expect(page.locator('#account-error')).to_contain_text('Test download failed')
                page.get_by_role('button', name='Retry setup', exact=True).click()
                expect(page.locator('#git-login-output')).to_contain_text('TEST-2468')
                expect(page.locator('#account-tool-progress')).to_contain_text('GitHub CLI · Ready')
                expect(page.locator('#account-tool-progress')).to_contain_text('Dev Tunnels · Ready')
                (root / 'github/login-ready').touch()
                expect(page.locator('#tunnel-login-output')).to_contain_text('TEST-1234')
                (root / 'tunnels/login-ready').touch()
                expect(page.locator('#git-login')).to_have_text('GitHub connected')
                expect(page.locator('#account-error')).to_be_hidden()
                assert all((root / 'state/tools' / name).is_file() for name in ['gh','devtunnel'])
                assert len(attempts) == 3
                assert str(root / 'state/tools') in app.extensions['gusnotebook'].git.auth.environment()['PATH'].split(os.pathsep)
                app.config['PREVIEW_SINGLE_PORT'] = True
                response = context.request.post(url + 'api/accounts', data={'setup':True})
                assert response.status == 400
                app.config['PREVIEW_SINGLE_PORT'] = False
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-setup-complete.png'))
                print('PASS: setup progress, cancel, retry and automatic continuation through both sign-ins', flush=True)
            with server(root) as (_app, url):
                authenticate_browser(context, url)
                page.goto(url)
                page.locator('#accounts-button').click()
                expect(page.locator('#git-login')).to_have_text('GitHub connected')
                assert len(attempts) == 3
                assert not errors, errors
                print('PASS: installed tools are rediscovered after restart; remote installation is blocked', flush=True)
            browser.close()


if __name__ == '__main__':
    main()
