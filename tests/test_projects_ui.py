"""Real browser report → isolated PCA → review → notebook, on a disposable server.

Run directly with Docker running. No model credentials or live model calls.
"""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ["GUSNOTEBOOK_TEST_URL"]
    work = Path(os.environ["GUSNOTEBOOK_TEST_ROOT"]).resolve() / "work"
    source = work / "measurements.csv"
    source.write_text("x,y,z\n1,8,3\n2,4,9\n3,7,5\n4,3,1\n5,2,8\n6,6,4\n")
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        authenticate_browser(page.context, url)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("booted")
        page.locator('#tab-new').click()
        page.get_by_role('menuitem', name='Analysis project').click()
        expect(page.locator('#project-workspace')).to_be_visible()
        page.locator('#project-name').fill('browser_study')
        page.locator('#project-location').fill(str(work))
        page.get_by_role('button', name='Create project', exact=True).click()
        expect(page.locator('#project-body')).to_be_visible()
        page.locator('#project-source-id').fill('measurements')
        page.locator('#project-source-path').fill(str(source))
        page.get_by_role('button', name='Approve file', exact=True).click()
        expect(page.locator('#project-sources')).to_contain_text('measurements')
        page.locator('#project-prepare').evaluate('(el) => el.closest("details").open = true')
        page.locator('#project-prepare').click()
        expect(page.locator('#project-status')).to_contain_text('prepare: completed', timeout=180000)
        page.locator('#project-parameters').fill(json.dumps({'source':'measurements','columns':['x','y','z']}))
        page.get_by_role('button', name='Save analysis', exact=True).click()
        expect(page.locator('#project-status')).to_have_text('Analysis saved.')
        page.locator('#project-run').click()
        expect(page.locator('#project-status')).to_contain_text('run: completed', timeout=30000)
        page.locator('#project-candidate').click()
        frame = page.frame_locator('#project-preview')
        expect(frame.locator('svg circle')).to_have_count(6)
        expect(frame.get_by_role('heading', name='Principal component analysis')).to_be_visible()
        page.locator('#project-accept').click()
        expect(page.locator('#project-status')).to_have_text('Result accepted into report.')
        expect(frame.locator('svg circle')).to_have_count(6)
        frame.locator('.analysis-link').click()
        expect(page.locator('#project-receipt')).to_be_visible()
        expect(page.locator('#project-receipt')).to_contain_text('scikit-learn')
        page.locator('#project-replay').click()
        expect(page.locator('#project-status')).to_contain_text('replay: completed', timeout=30000)
        expect(page.locator('#project-comparison')).to_contain_text('Results match')
        page.locator('#project-edit-report').click()
        expect(page.locator('#project-html')).to_be_visible()
        source_html = page.locator('#project-html').input_value()
        page.locator('#project-html').fill(source_html.replace('Write your story here.', 'A human reviewed this cohort.'))
        page.locator('#project-save-report').click()
        expect(frame.get_by_text('A human reviewed this cohort.', exact=False)).to_be_visible()
        page.locator('#project-package').click()
        expect(page.locator('#project-status')).to_contain_text('package: completed', timeout=60000)
        expect(page.locator('#project-comparison')).to_contain_text('Package verified')
        page.screenshot(path='/tmp/gusnb-project-workspace.png')
        page.locator('#project-close').click()
        page.evaluate('AppAppearance.toggle()')
        page.evaluate('ProjectWorkspace.open()')
        expect(page.locator('#project-body')).to_be_visible()
        page.locator('#project-chat-settings').click()
        expect(page.locator('#settings-projects')).to_be_visible()
        expect(page.get_by_label('Gateway base URL', exact=True)).to_be_visible()
        expect(page.get_by_label('Model deployment', exact=True)).to_be_visible()
        page.get_by_role('button', name='Close settings', exact=True).click()
        page.evaluate('ProjectWorkspace.open()')
        expect(page.locator('#project-body')).to_be_visible()
        page.locator('#project-notebook').click()
        page.wait_for_function("active.endsWith('/notebooks/analysis.ipynb')")
        notebook = json.loads((work / 'browser_study/notebooks/analysis.ipynb').read_text())
        assert notebook['cells'][1]['outputs'][0]['data']['application/json']['metrics']['rows_analyzed'] == 6
        assert not errors, errors
        browser.close()
    print('Project browser workflow passed')


if __name__ == '__main__':
    main()
