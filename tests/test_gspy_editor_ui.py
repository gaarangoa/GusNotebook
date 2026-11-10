"""Real Quill typing, Python embeds, background execution, undo and persistence."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from ui_server import authenticate_browser, launch_browser, rerun_isolated


def main():
    rerun_isolated(__file__)
    url = os.environ['GUSNOTEBOOK_TEST_URL']
    path = Path(os.environ['GUSNOTEBOOK_TEST_ROOT']) / 'work' / 'editor.gspy'
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined});")
        authenticate_browser(context, url)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('#tab-new')
        page.click('#tab-new')
        page.get_by_role('menuitem', name='Scientific notebook').click()
        page.fill('#ask-input', 'editor.gspy')
        page.click('#ask-ok')
        editor = page.get_by_role('textbox', name='Scientific document')
        expect(editor).to_be_visible()
        assert page.evaluate('activeTab().gspyEditor.quill instanceof GspyQuill')
        document_path = page.evaluate('active')
        editor.click()
        page.keyboard.type('Study results')
        page.get_by_label('Text style', exact=True).select_option('1')
        page.keyboard.press('End')
        page.keyboard.press('Enter')
        page.keyboard.type('Analysis starts here.')
        expect(editor.locator('h1')).to_have_text('Study results')
        page.evaluate('() => saveGspyDocument()')
        assert 'Study results' in path.read_text()
        page.keyboard.press('Enter')
        page.get_by_role('button', name='+ Python', exact=True).click()
        panel = page.locator('.gspy-inspector')
        expect(panel).to_be_visible()
        cid = page.evaluate('activeTab().gspyEditor.inspector')
        page.evaluate('id => { const v=cmViews.get(id); v.dispatch({changes:{from:0,to:v.state.doc.length,insert:"seed = 40\\nprint(\'background log\')"}}); }', cid)
        page.get_by_label('Python output mode').select_option('background')
        panel.get_by_role('button', name='Run', exact=True).click()
        page.wait_for_function('id => getCell(id).content.run?.state === "fresh"', arg=cid)
        expect(editor).not_to_contain_text('background log')
        panel.locator('details').evaluate('e => e.open = true')
        expect(panel.locator('.output-area')).to_contain_text('background log')
        panel.get_by_label('Close Python inspector').click()
        # Insert a second computation after the first; both share one kernel.
        page.evaluate('() => {const q=activeTab().gspyEditor.quill; q.setSelection(q.getLength()-1);}')
        page.keyboard.type('The calculation gives:')
        page.keyboard.press('Enter')
        page.get_by_role('button', name='+ Python', exact=True).click()
        page.wait_for_function('id => activeTab().gspyEditor.inspector && activeTab().gspyEditor.inspector !== id && cmViews.has(activeTab().gspyEditor.inspector)', arg=cid)
        second = page.evaluate('activeTab().gspyEditor.inspector')
        page.evaluate('id => { const v=cmViews.get(id); v.dispatch({changes:{from:0,to:v.state.doc.length,insert:"print(seed + 2)"}}); }', second)
        panel.get_by_role('button', name='Run through here', exact=True).click()
        expect(editor.locator('.output.stream')).to_contain_text('42')
        panel.get_by_label('Close Python inspector').click()
        # The inspector remounts its code editor when returning from a
        # regular notebook, and Shift+Enter returns to writing after the step.
        page.evaluate('id => openGspyPython(id)', second)
        ordinary = page.evaluate('tabs.find(t=>t.format==="ipynb").path')
        page.evaluate('path => switchTab(path)', ordinary)
        page.evaluate('path => switchTab(path)', document_path)
        page.wait_for_function('id => cmViews.has(id) && document.getElementById("ed-"+id)?.contains(cmViews.get(id).dom)', arg=second)
        page.evaluate('id => focusCellEditor(id)', second)
        page.keyboard.press('Shift+Enter')
        expect(panel).to_be_hidden()
        expect(editor).to_be_focused()
        page.wait_for_function('id => !getCell(id)._running', arg=second)
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'gusnb-quill.png'))
        # Splitting prose with Python preserves both halves and undo merges
        # the paragraph again without losing its formatting.
        page.evaluate('() => {const q=activeTab().gspyEditor.quill; const start=q.getText().indexOf("Analysis starts here."); q.setSelection(start+8);}')
        page.get_by_role('button', name='+ Python', exact=True).click()
        expect(panel).to_be_visible()
        page.wait_for_function('id => activeTab().gspyEditor.inspector && activeTab().gspyEditor.inspector !== id', arg=second)
        split = page.evaluate('activeTab().gspyEditor.inspector')
        assert split not in (cid, second)
        panel.get_by_label('Close Python inspector').click()
        page.evaluate('() => activeTab().gspyEditor.quill.history.undo()')
        page.evaluate('() => saveGspyDocument()')
        expect(editor).to_contain_text('Analysis starts here.')
        assert not page.evaluate('id => !!getCell(id)', split)
        # A deletion archives even the code typed just before it, before its
        # own debounce has saved, so undo can restore that exact source.
        page.evaluate('id => openGspyPython(id)', second)
        page.evaluate('id => {const v=cmViews.get(id); v.dispatch({changes:{from:v.state.doc.length,insert:"\\n# Recorded before deletion"}});}', second)
        # Delete, autosave, and undo a Python embed: restore archived code.
        page.evaluate('id => {const q=activeTab().gspyEditor.quill; const b=q.getLines().find(b=>b.domNode.dataset.pythonId===id); q.history.cutoff(); q.deleteText(q.getIndex(b),1,"user"); q.history.cutoff();}', second)
        page.evaluate('() => saveGspyDocument()')
        assert not page.evaluate('id => !!getCell(id)', second)
        page.evaluate('() => activeTab().gspyEditor.quill.history.undo()')
        page.evaluate('() => saveGspyDocument()')
        assert page.evaluate('id => getCell(id).source', second) == 'print(seed + 2)\n# Recorded before deletion'
        # Literal HTML stays literal text; imports cannot execute script.
        page.evaluate('() => {const q=activeTab().gspyEditor.quill; q.insertText(q.getLength()-1, "Literal <img src=x onerror=window.injected=1> text", "user");}')
        page.evaluate('() => saveGspyDocument()')
        page.reload(wait_until='domcontentloaded')
        expect(editor).to_contain_text('Literal <img src=x onerror=window.injected=1> text')
        expect(editor.locator('h1')).to_have_text('Study results')
        expect(editor.locator('.gspy-python.background')).to_have_count(1)
        assert not page.evaluate('window.injected')
        assert page.evaluate('id => getCell(id).source', second) == 'print(seed + 2)\n# Recorded before deletion'
        # Python may finish while prose is being edited; it must not reset
        # the Quill instance, selection or undo stack.
        source = "import time\ntime.sleep(0.8)\nprint(seed + 3)"
        response = context.request.patch(url + 'api/cells/' + second, params={'notebook': str(path)}, data={'source': source})
        assert response.ok, response.text()
        page.wait_for_function('args => getCell(args.id).source === args.source', arg={'id': second, 'source': source})
        page.evaluate('id => {window.editorBefore=activeTab().gspyEditor.quill; window.pendingRun=runCell(id);}', second)
        page.wait_for_function('id => getCell(id)._running', arg=second)
        page.evaluate('() => {const q=activeTab().gspyEditor.quill; q.setSelection(q.getLength()-1); q.history.cutoff();}')
        page.keyboard.type(' Still writing during execution.')
        caret = page.evaluate('activeTab().gspyEditor.quill.getSelection().index')
        page.evaluate('() => window.pendingRun')
        assert page.evaluate('activeTab().gspyEditor.quill === window.editorBefore')
        assert page.evaluate('activeTab().gspyEditor.quill.getSelection().index') == caret
        expect(editor).to_contain_text('Still writing during execution.')
        page.evaluate('() => saveGspyDocument()')
        page.evaluate('() => activeTab().gspyEditor.quill.history.undo()')
        expect(editor).not_to_contain_text('Still writing during execution.')
        page.evaluate('() => saveGspyDocument()')
        # A failed save retains the draft. Concurrent code edits are merged,
        # but conflicting narrative edits must be surfaced without data loss.
        page.route('**/api/notebook/document?*', lambda route: route.abort())
        page.evaluate('() => {const q=activeTab().gspyEditor.quill; q.insertText(q.getLength()-1," Draft kept.","user");}')
        page.evaluate('() => saveGspyDocument().catch(() => {})')
        expect(page.locator('.gspy-editor-status')).to_contain_text('Save failed')
        assert page.evaluate('activeTab().dirty')
        latest = "print('new code from agent')"
        response = context.request.patch(url + 'api/cells/' + second, params={'notebook': str(path)}, data={'source': latest})
        assert response.ok, response.text()
        page.wait_for_function('args => getCell(args.id).source === args.source', arg={'id': second, 'source': latest})
        expect(editor).to_contain_text('Draft kept.')
        page.unroute('**/api/notebook/document?*')
        page.evaluate('() => saveGspyDocument()')
        assert ''.join(next(c for c in json.loads(path.read_text())['cells'] if c['id'] == second)['source']) == latest
        assert any('&lt;img' in ''.join(c['source']) for c in json.loads(path.read_text())['cells'] if c['cell_type'] == 'markdown')
        page.route('**/api/notebook/document?*', lambda route: route.abort())
        page.evaluate('() => {const q=activeTab().gspyEditor.quill; q.insertText(q.getLength()-1," Unmerged local prose.","user");}')
        page.evaluate('() => saveGspyDocument().catch(() => {})')
        narrative = page.evaluate('cells.filter(c=>c.cell_type==="markdown").at(-1).id')
        response = context.request.patch(url + 'api/cells/' + narrative, params={'notebook': str(path)}, data={'source': 'New server prose'})
        assert response.ok, response.text()
        page.wait_for_function('id => getCell(id).source === "New server prose"', arg=narrative)
        expect(editor).to_contain_text('Unmerged local prose.')
        page.unroute('**/api/notebook/document?*')
        page.evaluate('() => saveGspyDocument().catch(() => {})')
        expect(page.locator('.gspy-editor-status')).to_contain_text('changed elsewhere')
        assert page.evaluate('activeTab().dirty')
        assert ''.join(next(c for c in json.loads(path.read_text())['cells'] if c['id'] == narrative)['source']) == 'New server prose'
        saved = json.loads(path.read_text())
        events = saved['metadata']['gusnotebook']['journal']
        assert any(e['kind'] == 'delete' for e in events)
        assert any(e['kind'] == 'restore' for e in events)
        assert not errors, errors
        browser.close()
    print('Quill prose, formatting, Python output/background, ordered execution, undo and persistence passed.')


if __name__ == '__main__':
    main()
