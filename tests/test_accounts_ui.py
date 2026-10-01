"""Accounts at the rail bottom, both authorization steps, reload and narrow UI."""

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, isolated_server, launch_browser


def main():
    with tempfile.TemporaryDirectory(prefix="gusnb-accounts-ui-") as temporary:
        root = Path(temporary)
        github, tunnels = root / "github", root / "tunnels"
        github.mkdir()
        tunnels.mkdir()
        for name, fixture in [("gh", "fake_gh.py"), ("devtunnel", "fake_devtunnel.py")]:
            executable = root / name
            executable.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name(fixture).read_text())
            executable.chmod(0o755)
        (tunnels / "account.json").write_text('{"status":"Logged out"}')
        (tunnels / "device-login").touch()
        environment = {"GUSNOTEBOOK_GH": str(root / "gh"), "GUSNOTEBOOK_FAKE_GH": str(github),
            "GUSNOTEBOOK_DEVTUNNEL": str(root / "devtunnel"), "GUSNOTEBOOK_FAKE_TUNNEL": str(tunnels),
            "GUSNOTEBOOK_FAKE_TUNNEL_USERNAME": "researcher", "GH_TOKEN": "", "GITHUB_TOKEN": ""}
        with patch.dict(os.environ, environment), isolated_server() as (url, token, _root, _env), sync_playwright() as playwright:
            os.environ["NB_TOKEN"] = token
            browser = launch_browser(playwright)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            authenticate_browser(context, url)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            accounts = page.locator('#accounts-button')
            popup = page.locator('#accounts-popover')
            expect(accounts).to_be_visible()
            box = accounts.bounding_box()
            assert box['y'] + box['height'] >= 890
            accounts.click()
            page.get_by_role('button', name='Sign in with GitHub', exact=True).click()
            expect(page.locator('#git-login-output')).to_contain_text('TEST-2468')
            page.keyboard.press('Escape')
            expect(popup).to_be_hidden()
            expect(accounts).to_be_focused()
            (github / 'login-ready').touch()
            # Reload while the server continues to the second authorization.
            page.reload()
            accounts.click()
            expect(page.locator('#tunnel-login-output')).to_contain_text('TEST-1234')
            expect(page.get_by_role('link', name='Open sign-in page')).to_have_attribute('href', 'https://github.com/login/device')
            page.get_by_role('button', name='Cancel sign-in', exact=True).click()
            expect(page.locator('#tunnel-login-output')).to_be_hidden()
            expect(page.locator('#git-account')).to_have_text('GitHub · researcher')
            page.get_by_role('button', name='Connect GitHub to tunnels', exact=True).click()
            expect(page.locator('#tunnel-login-output')).to_contain_text('TEST-1234')
            expect(page.locator('#git-login-output')).to_be_hidden()
            (tunnels / 'login-ready').touch()
            expect(page.locator('#git-login')).to_have_text('GitHub connected')
            expect(page.locator('#tunnel-account')).to_have_text('Tunnels · GitHub · researcher')
            expect(page.locator('#account-error')).to_be_hidden()
            expect(page.locator('#account-login-cancel')).to_be_hidden()
            for width in [1440, 768, 390, 320]:
                page.set_viewport_size({'width': width, 'height': 900})
                box = popup.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                assert box['y'] >= 0 and box['y'] + box['height'] <= 900
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width': 1440, 'height': 900})
            for theme in ['light', 'dark']:
                page.evaluate('theme => AppAppearance.update({theme})', theme)
                page.screenshot(path=str(Path(tempfile.gettempdir()) / f'gusnb-accounts-{theme}.png'))
            page.keyboard.press('Escape')
            for panel in ['git', 'tunnels']:
                page.locator('#sidebar-' + panel).click()
                assert page.locator('#git-pane' if panel == 'git' else '#tunnels').get_by_role('button', name='Sign in', exact=False).count() == 0
            assert not errors, errors
            assert 'gho_TEST_ONLY_NEVER_VALID' not in json.dumps(page.evaluate("api('/api/accounts')"))
            browser.close()
            print('PASS: one account control, both authorization steps, cancellation, reload, themes and mobile widths')


if __name__ == '__main__':
    main()
