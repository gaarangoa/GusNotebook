/* The two strips at the foot of the left panel — Skills and Sessions — and
 * boot().
 *
 * boot() is last in this file and calls into every file before it, which is
 * what fixes the load order: it must not run until the functions it opens tabs
 * with exist. */

/* ---------- Skills ----------
 * A skill is one markdown file: a description, a note on when (and when not) to
 * use it, and a code block. Two consumers read the same file, which is the whole
 * point of the format — write the practice once and both benefit:
 *
 *   Claude   — the directory is passed as --plugin-dir, so /csv-peek works and
 *              Claude can cite the practice while writing code.
 *   Notebook — clicking a skill inserts its first code block as a cell. No model
 *              call: if you already know which snippet you want, waiting on a
 *              generation to reproduce it is a step backwards.
 *
 * Scope is a deliberate limit. Snippets and practices, not frameworks: code with
 * its own dependencies belongs in an importable module where it can be versioned
 * and tested, not pasted out of markdown into a cell.
 */
let skillList = [];
let editingSkill = null;      // the id being edited, or null for a new one

function toggleSkills() {
  document.getElementById('skills').classList.toggle('collapsed');
}

async function loadSkills() {
  try {
    const data = await api('/api/skills');
    skillList = data.skills || [];
  } catch (err) { skillList = []; }
  renderSkills();
}

function renderSkills() {
  // A count in the shut header, matching how Sessions names the current one:
  // the strip has to say whether there's anything in it before you open it.
  document.getElementById('skills-cur').textContent =
    skillList.length ? `${skillList.length}` : '';

  document.getElementById('skill-list').innerHTML = skillList.map(s => `
    <div class="skill-row" onclick="insertSkill('${escapeAttr(s.id)}')"
         title="${escapeAttr(s.description || s.name)}&#10;&#10;click to add as a cell · ▤ to edit">
      <span class="ic">◈</span>
      <span class="nm">${escapeHtml(s.name)}</span>
      <span class="ds">${escapeHtml(s.description || '')}</span>
      <span class="ed" onclick="openSkill('${escapeAttr(s.id)}', event)"
            title="Edit this skill">▤</span>
    </div>`).join('') ||
    // Not just "none": an empty list has to say what the thing is for, since
    // there's no example on screen to infer it from.
    '<div class="skill-empty">No skills yet. A skill is a snippet plus when to ' +
    'use it — click <b>+</b> to write one. Claude reads them too.</div>';
}

/** Put a skill's code in a new cell below the current one.
 *
 *  A code cell, run by the user rather than automatically: a snippet is a
 *  starting point that usually needs a path or a column name changed first, and
 *  running it unedited would just produce a NameError. */
async function insertSkill(sid) {
  const s = skillList.find(x => x.id === sid);
  if (!s) return;
  if (!activeTab() || activeTab().kind !== 'notebook') {
    flash('Open a notebook to add a skill to it.');
    return;
  }
  if (!s.code) {
    flash(`"${s.name}" has no code block — ▤ to add one.`);
    return;
  }
  const cell = await addCell('code', selected);
  if (!cell) return;
  const ed = document.getElementById('ed-' + cell.id);
  if (ed) {
    ed.value = s.code;
    autosize(ed);
    ed.focus();
  }
  // Saved so the cell survives a reload even if the user runs nothing: addCell
  // created an empty cell server-side, and the code so far is only in the DOM.
  await saveCell(cell.id);
  flash(`Added "${s.name}" — edit it, then ⇧⏎ to run`);
}

function openSkill(sid, ev) {
  if (ev) ev.stopPropagation();       // the row itself inserts the code
  const s = sid ? skillList.find(x => x.id === sid) : null;
  editingSkill = s ? s.id : null;
  document.getElementById('skill-title').textContent =
    s ? `Skill · ${s.name}` : 'New skill';
  document.getElementById('skill-name').value = s ? s.name : '';
  document.getElementById('skill-desc').value = s ? s.description : '';
  document.getElementById('skill-body').value = s ? s.body : '';
  // Nothing to delete when it doesn't exist yet.
  document.getElementById('skill-del').style.display = s ? '' : 'none';
  previewSkillId();
  document.getElementById('skill-back').classList.add('on');
  setTimeout(() => {
    const el = document.getElementById(s ? 'skill-body' : 'skill-name');
    el.focus();
    // Focusing puts the caret at the end, which scrolls a long body past the
    // "when to use it" note at the top — the part most worth reading first.
    el.setSelectionRange(0, 0);
    el.scrollTop = 0;
  }, 30);
}

function newSkill(ev) {
  if (ev) ev.stopPropagation();       // the header toggles the list
  openSkill(null);
}

/** Mirror skills.py's _slug, so the name you type shows the /command you'll get.
 *  Duplicated rather than fetched: it's four characters of rule, and a round
 *  trip per keystroke to learn that a space becomes a dash isn't worth it. */
function previewSkillId() {
  const raw = document.getElementById('skill-name').value;
  const slug = raw.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-')
                  .replace(/^-+|-+$/g, '');
  document.getElementById('skill-id-preview').textContent = '/' + (slug || 'name');
}

function closeSkill() {
  document.getElementById('skill-back').classList.remove('on');
  editingSkill = null;
}

async function saveSkill() {
  const name = document.getElementById('skill-name').value.trim();
  if (!name) { flash('A skill needs a name.'); return; }
  const body = {
    name,
    description: document.getElementById('skill-desc').value.trim(),
    body: document.getElementById('skill-body').value,
  };
  if (editingSkill) body.id = editingSkill;
  try {
    await api('/api/skills', {method: 'POST', body: JSON.stringify(body)});
  } catch (err) { flash('Cannot save the skill: ' + errText(err)); return; }
  closeSkill();
  await loadSkills();
  // Said plainly, because it's surprising otherwise: a plugin's skills are read
  // when Claude starts, so a terminal already open won't know about this one.
  flash(`Saved "${name}" — new Claude sessions will see it`);
}

async function deleteSkill() {
  if (!editingSkill) return;
  const s = skillList.find(x => x.id === editingSkill);
  const ok = await askConfirm(`Delete skill "${s ? s.name : editingSkill}"?`,
                              'The markdown file is removed.', 'Delete');
  if (!ok) return;
  try {
    await api('/api/skills/' + encodeURIComponent(editingSkill),
              {method: 'DELETE'});
  } catch (err) { flash('Cannot delete: ' + errText(err)); return; }
  closeSkill();
  await loadSkills();
}

/* ---------- Sessions ----------
 * A session is a named group of tabs with a root directory — a way to keep
 * separate projects off the same page. Switching is a *view* change: the server
 * keeps every session's documents open and its kernels running, so a training
 * run in one session survives you working in another, and you come back to a
 * live kernel rather than a cold one.
 *
 * That's why the row shows what's live elsewhere. Nothing else in the UI would
 * tell you a kernel is still burning CPU in a session you can't see.
 */
let sessionList = [];
let currentSession = null;

function toggleSessions() {
  document.getElementById('sessions').classList.toggle('collapsed');
}

/* Kernel status fires on every idle/busy transition, i.e. once per cell run.
 * Coalesce, so a loop of quick cells doesn't mean a request per cell. */
let countTimer = null;

function refreshSessionCounts() {
  if (countTimer) return;
  countTimer = setTimeout(() => { countTimer = null; loadSessions(); }, 400);
}

async function loadSessions() {
  try {
    const data = await api('/api/sessions');
    sessionList = data.sessions || [];
    currentSession = data.current;
  } catch (err) { sessionList = []; }
  renderSessions();
}

function renderSessions() {
  // The header carries the current session's name and what's live in it, so a
  // shut list still answers "where am I, and is anything running?".
  const cur = sessionList.find(s => s.current);
  const live = cur ? (cur.kernels || 0) + (cur.terminals_live || 0) : 0;
  document.getElementById('sessions-cur').textContent =
    cur ? cur.name + (live ? ` · ${live} live` : '') : '';

  document.getElementById('session-list').innerHTML = sessionList.map(s => {
    // Counts, not just names: "2 kernels live" is the point of not tearing down.
    const bits = [];
    if (s.tabs.length) bits.push(`${s.tabs.length}t`);
    if (s.kernels) bits.push(`<span class="live">${s.kernels}k</span>`);
    if (s.terminals_live) bits.push(`<span class="live">${s.terminals_live}✳</span>`);
    return `
    <div class="session-row ${s.current ? 'current' : ''}"
         onclick="switchSession('${escapeAttr(s.id)}')"
         ondblclick="renameSession('${escapeAttr(s.id)}', event)"
         title="${escapeAttr(s.root)}&#10;double-click to rename">
      <span class="ic">${s.current ? '▾' : '▸'}</span>
      <span class="nm">${escapeHtml(s.name)}</span>
      <span class="meta">${bits.join(' ')}</span>
      <span class="note ${s.instructions || hasRestrictions(s.restrictions) ? 'set' : ''}"
            onclick="openSessionInstr('${escapeAttr(s.id)}', event)"
            title="${s.instructions
                     ? 'Instructions for agents in this session:&#10;' + escapeAttr(s.instructions)
                     : 'Instructions for agents in this session'}${
                   hasRestrictions(s.restrictions)
                     ? '&#10;&#10;Restricted: ' + Object.keys(s.restrictions).length +
                       ' setting(s)' : ''}">▤</span>
      <span class="x" onclick="deleteSession('${escapeAttr(s.id)}', event)"
            title="Close this session — its tabs and kernels shut down">✕</span>
    </div>`;
  }).join('') ||
    '<div class="files-msg">no sessions</div>';
}

async function switchSession(sid) {
  if (sid === currentSession) return;
  stashActive();                     // don't lose an unsaved text edit
  try {
    await api('/api/sessions/' + encodeURIComponent(sid),
              {method: 'POST', body: JSON.stringify({switch: true})});
  } catch (err) { flash('Cannot switch session: ' + errText(err)); return; }
  await reloadWorkspace();
}

/** Repaint tabs, terminals and the file tree for whatever session is current.
 *
 *  Local tab state is dropped and rebuilt from the server rather than filtered
 *  in place: the server is what knows which documents belong to the session, and
 *  a stale local tab would point at a document this session doesn't own. */
async function reloadWorkspace() {
  // Detach each xterm individually — #term-empty lives inside the stack, and
  // emptying the whole container would delete the empty-state element with it.
  for (const t of terms) {
    try { t.ws && t.ws.close(); } catch (e) {}
    try { t.term.dispose(); } catch (e) {}
    if (t.host) t.host.remove();
  }
  terms = [];
  activeTerm = null;
  tabs = [];
  active = null;
  cells = [];
  let info = {};
  try { info = await api('/api/tabs'); } catch (err) { info = {}; }
  // loadSessions() first: browse() records where you are onto the current
  // session, and until this lands `currentSession` is still the one we left —
  // which would write the new session's root onto the old one.
  await loadSessions();
  for (const t of (info.tabs || [])) await openFile(t.path);
  // An empty session paints nothing on its own — openFile() is what normally
  // repaints, and it never ran.
  if (!tabs.length) { renderTabs(); showActive(); }
  await browse(info.session_root || null);
  await bootTerminals();
}

async function newSession(ev) {
  if (ev) ev.stopPropagation();       // the header itself toggles the list
  const name = await askName('New session', '', 'its own tabs and kernels');
  if (name === null || !name.trim()) return;
  try {
    await api('/api/sessions', {method: 'POST', body: JSON.stringify(
      {name: name.trim(), root: fileState.path || undefined, switch: true})});
  } catch (err) { flash('Cannot create the session: ' + errText(err)); return; }
  await reloadWorkspace();
  flash(`Session "${name.trim()}" — empty, rooted in ${fileState.path}`);
}

async function renameSession(sid, ev) {
  if (ev) ev.stopPropagation();
  const s = sessionList.find(x => x.id === sid);
  const name = await askName('Rename session', s ? s.name : '', '', 'Rename');
  if (name === null || !name.trim()) return;
  try {
    await api('/api/sessions/' + encodeURIComponent(sid),
              {method: 'POST', body: JSON.stringify({name: name.trim()})});
  } catch (err) { flash('Cannot rename: ' + errText(err)); return; }
  await loadSessions();
}

async function deleteSession(sid, ev) {
  if (ev) ev.stopPropagation();
  const s = sessionList.find(x => x.id === sid);
  // Deleting is the one destructive path — it shuts kernels and PTYs down,
  // because a session you can't see must not leave them running unreachably.
  const live = s ? (s.kernels || 0) + (s.terminals_live || 0) : 0;
  const warn = live ? `${live} live kernel/terminal(s) will stop. ` : '';
  const ok = await askConfirm(`Close session "${s ? s.name : sid}"?`,
                              warn + 'Its tabs close; the files stay on disk.',
                              'Close session');
  if (!ok) return;
  try {
    await api('/api/sessions/' + encodeURIComponent(sid), {method: 'DELETE'});
  } catch (err) { flash('Cannot close the session: ' + errText(err)); return; }
  await reloadWorkspace();
}

/* Per-session instructions for agents. Kept on the session rather than in
 * Settings because a session is a project: "never touch prod/" is true of one
 * repository and meaningless in another. */
let instrSession = null;

async function openSessionInstr(sid, ev) {
  if (ev) ev.stopPropagation();       // the row itself switches session
  const s = sessionList.find(x => x.id === sid);
  if (!s) return;
  instrSession = sid;
  document.getElementById('sinstr-title').textContent = `Agents · ${s.name}`;
  document.getElementById('sinstr-text').value = s.instructions || '';
  document.getElementById('sinstr-back').classList.add('on');
  setTimeout(() => document.getElementById('sinstr-text').focus(), 30);

  // After the reveal, not before: the presets may need a fetch if Settings has
  // never been opened, and the instructions half shouldn't wait on it.
  await loadRestrictionPresets();
  if (instrSession !== sid) return;         // the modal was closed meanwhile
  const restrict = s.restrictions || {};
  renderRestrictions(document.getElementById('sinstr-restrict'), restrict);
  document.getElementById('sinstr-restrict-extra').value = restrict.deny_extra || '';
}

function closeSessionInstr() {
  document.getElementById('sinstr-back').classList.remove('on');
  instrSession = null;
}

async function saveSessionInstr() {
  if (!instrSession) return;
  const text = document.getElementById('sinstr-text').value;
  const restrict = collectRestrictions(
    document.getElementById('sinstr-restrict'),
    document.getElementById('sinstr-restrict-extra'));
  const sid = instrSession;
  try {
    await api('/api/sessions/' + encodeURIComponent(sid),
              {method: 'POST',
               body: JSON.stringify({instructions: text, restrictions: restrict})});
  } catch (err) { flash('Cannot save: ' + errText(err)); return; }
  closeSessionInstr();
  await loadSessions();
  flash(text.trim() || hasRestrictions(restrict)
    ? 'Saved — new agent terminals in this session will follow it'
    : 'Session instructions cleared');
}

/** First paint. Restores whatever the server still has open, so a browser
 *  reload doesn't collapse a set of tabs back to one. */
async function boot() {
  let open = [];
  let primary = null;
  let root = null;
  try {
    const t = await api('/api/tabs');
    open = t.tabs || [];
    primary = t.primary;
    root = t.session_root;
  } catch (err) { /* fall back to the single default notebook below */ }

  // Only when the server offered nothing at all. An empty session is a real
  // state — a new one starts with no tabs — and shouldn't summon the primary
  // notebook into it.
  if (!open.length && !root) open = [{path: primary, kind: 'notebook'}];

  // Before browse(), which records the browsed directory onto whatever
  // currentSession says — null until this has loaded.
  await loadSessions();

  // Open the primary notebook first so it lands leftmost and gets focus.
  open.sort((a, b) => (a.path === primary ? -1 : b.path === primary ? 1 : 0));
  for (const t of open) await openFile(t.path);
  if (primary && tab(primary)) switchTab(primary);
  await browse(root);  // the session's root, or the notebook's own directory
  // The .* button is lit from toggleHidden() onwards; light it here too so it
  // matches the starting state instead of claiming dotfiles are off.
  document.getElementById('hidden-btn').classList.toggle('on', fileState.hidden);
  await loadSessions();   // refetch: opening those tabs changed the counts
  await loadSkills();
  booted = true;       // anything waiting on first paint can watch this
}

let booted = false;
boot();
