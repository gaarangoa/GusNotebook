# GusNotebook

A two-pane notebook: your notebooks on the left, embedded Claude Code or Codex
agents on the right. Files open in tabs, each open notebook gets its own IPython
kernel, and either agent can read and rewrite **the cell you're parked on**
without being told which one it is.

HTML and SVG files open in an integrated visual browser rather than as source.
Each tab gets a short-lived server on its own localhost port, rooted beside the
file, so relative and root-relative assets, scripts, modules, and `fetch()` work
like a normal website. Closing the tab stops that server. Edit HTML text directly
on the rendered page, or double-click SVG text to change it in place; visual
changes save back to the original file through **Save** or ⌘S.

Select a rendered region before moving to a Claude or Codex terminal and that
exact range, its surrounding markup, and the document path are injected with
your next prompt. The agent edits that file directly on disk, changing only the
selected region while using the surrounding document as context. GusNotebook
watches the open file and reloads a clean visual canvas automatically after the
agent saves. If the canvas has an unsaved edit, it shows a conflict and asks
before discarding the browser version.

## Install

```bash
uv tool install gusnotebook       # or: pipx install gusnotebook
gusnotebook                       # serves the directory you're in
```

From a checkout:

```bash
uv sync                           # exact versions from uv.lock
uv run gusnotebook
```

Claude Code and Codex are optional external CLIs. Install and sign in to the
provider you want to select; GusNotebook launches the existing `claude` or
`codex` command rather than bundling either one.

`gusnotebook` serves **the directory you run it in**, the way `jupyter lab`
does. It opens on the first `.ipynb` it finds there, or creates
`notebook.ipynb`. Nothing else is ever written into your project.

Options: `--port`, `--host`, `--no-browser`. `NOTEBOOK=/path/to/x.ipynb` picks
the launch document.

By default, the launch link includes a fresh access token. Opening it unlocks
that browser; the token is removed from the address bar. API and terminal connections require
authentication, and requests from another website are rejected. Embedded
terminals receive authentication automatically. An external `gusnb` command
finds the current local connection in the app state directory, or accepts
`NB_URL` and `NB_TOKEN` explicitly.

To open directly without a token or password, pass `--no-auth`, or set
`GUSNOTEBOOK_NO_AUTH=1` in your service environment. For remote access:

```bash
uv run gusnotebook --host 0.0.0.0 --port 4477 \
  --allowed-host YOUR_SERVER_IP --no-auth --no-browser
```

Replace `YOUR_SERVER_IP` with the IP you use in the browser and open
`http://YOUR_SERVER_IP:4477/`. Anyone who can reach the server can then read or
modify files and run notebook or terminal commands, so use this mode on a
trusted, access-controlled network. Host and browser-origin checks still apply.

For a reverse proxy, pass `--trust-proxy` and `--allowed-host your-hostname`.
Forwarded headers are otherwise ignored. The proxy must also route WebSockets;
visual previews still use separate ports on the listening interface.

If the proxy forwards the public URL prefix unchanged, set
`APP_BASE_URL="/some/prefix"`. GusNotebook strips that prefix before routing and
keeps it in generated browser URLs and authentication cookies.

## Appearance and layout

Open **Settings → Appearance** for **Light**, **Dark**, or **Follow system**,
comfortable or compact spacing, and the code/terminal font size. Changes preview
immediately; **Save settings** keeps them in this browser, and **Cancel** or
**Escape** restores the previous appearance. **⋯ → Switch theme** in the tab row
switches directly between light and dark. Editors, terminals, tables, menus,
and dialogs follow the theme; authored plots, images, and HTML previews keep
their original colors.

Documents and workspace controls share a compact 38 px row. The controls at its
right show or hide **Files** and **Agents and terminals**, and open **Settings**.
The **⋯** menu contains **Focus notebook**, theme switching, **Change history**,
and **Reload notebook**. Right-click a tab for **Rename…**, or focus the tab and
press F2. Drag either panel divider to resize it; widths and
visibility are saved in this browser. Focused dividers also accept arrow keys
(Shift for larger steps), Home, and End. Double-click a divider to reset its
width, or use **Reset layout** in Appearance. On smaller screens, the panel
buttons open drawers that close with Escape or a click outside.

**Settings → Notebook** controls code folding and lists shortcuts.
**Settings → Agents** contains standing instructions and expandable permission
settings. Tabs support arrow-key navigation, the **+** menu works with the
keyboard, and dialogs keep keyboard focus inside until closed.

## Python environments

Choose **+ → Environment** to create an environment with uv. Enter its name,
choose an existing parent folder, and list packages one per line, for example:

```text
pandas
numpy>=2,<3
requests[socks]
```

Optionally choose a Python version (such as `3.12`) or an interpreter path;
leaving it blank uses the app's Python. Add local repository folders with
**Add folder…** or paste one absolute path per line. Repositories need a
`pyproject.toml` or `setup.py`. Editable installation is enabled by default,
so source changes are available without reinstalling. Paths refer to the
machine running GusNotebook, including when you access it through a proxy.

**Create environment** runs uv and shows its installation log. `ipykernel` is
included automatically. You can close the modal while creation continues, or
cancel to remove the incomplete environment. Existing folders are never
overwritten. Successful environments remain in the picker after restarting
the app. Choose **Use for [notebook]** to switch that notebook's interpreter;
this restarts its kernel, clearing live variables. Finish or stop running
cells before switching.

The modal's **Installed packages** tab lists package names, versions, and local
repository paths, with filtering and refresh. You can also click **Packages**
beside an environment in the notebook's environment menu, or browse to an
environment that wasn't discovered automatically.

Creation requires `uv` on the server's PATH or in `~/.local/bin`; set
`GUSNOTEBOOK_UV=/absolute/path/to/uv` for another location and restart the app.
Package inspection works without uv or pip. See uv's documentation for
[environment creation](https://docs.astral.sh/uv/pip/environments/) and
[package and local repository installation](https://docs.astral.sh/uv/pip/packages/).

## Where things live

Two directories, deliberately separate:

| | |
|---|---|
| **Your project** | wherever you ran `gusnotebook` — notebooks, data, `.env` |
| **App state** | `~/.config/gusnotebook/` — `settings.json`, `sessions.json`, `environments.json`, `skills/` |

State is out of the install directory because an installed package's directory
is read-only, shared between every project, and replaced on upgrade. Override
the location with `GUSNOTEBOOK_HOME`; `XDG_CONFIG_HOME` is honoured.

## `gusnb` — driving the notebook from a terminal

Installed alongside the app. It talks HTTP to the running server, so it works
from any directory and reaches the same live kernel the browser is using.

```bash
gusnb add - <<'PY'                # add a cell, run it, print the output
import pandas as pd
pd.DataFrame({"site": ["A", "B"], "n": [12, 7]})
PY

gusnb here                        # current cell or selected HTML/SVG region
gusnb here - --run                # replace a notebook cell and run it
gusnb undo <cell_id>              # put back what a replace overwrote
gusnb list                        # every cell, with outputs
gusnb tabs                        # what's open, and on which interpreter
```

With several notebooks open, `-n analysis.ipynb` targets one. `--help` has the
rest.

## The cell or document region you're on

Choose **Claude** or **Codex** beside **+ Agent**, click a cell, then type a
request — *"open the quarterly report and extract the site and n columns"*.
The cell's source/output—or the selected HTML/SVG region and its surrounding
document context—is injected **before** the agent reads the prompt (a
`UserPromptSubmit` hook running `gusnb here`), so there's no `/command` to
remember and no id to quote. The agent writes a cell with `gusnb here - --run`,
or edits the selected HTML/SVG file directly and saves it on disk. The visual
canvas notices the save and reloads automatically.

Replacing is destructive, so every replace pushes the old source onto that
cell's own undo stack — a **↶** appears on the cell, one step per replace,
independent per cell, exactly as in JupyterLab. It's stored in cell metadata, so
it survives a restart and travels with the cell.

**History** in the title bar groups changes to the notebook and text tabs open
when an agent request begins. Open it to review the diffs, finish a recording,
and undo the recorded changes together. You can also start a manual recording.
History keeps the last 20 completed/active groups in app state, with up to 10 MB
of initial document content per group. Hidden files, unsupported files, and
documents beyond that limit are skipped. Files opened after a recording begins
are covered by the next recording.

A recording captures changes since a request; another editor or agent working
on the same files can contribute changes too. Undo refuses to overwrite files
that changed after the recording finished. It restores files, including saved
notebook outputs, while leaving live kernel variables intact.

Cell saves retain pending edits until the server acknowledges them. If an agent
changes the same cell meanwhile, saving reports a conflict and keeps your draft.
Unreadable notebooks are preserved on disk: repair the file and use **Reload**
before continuing. Renaming an idle notebook keeps its kernel and variables;
finish or stop a running cell before renaming or moving its file.

## Credentials

One gateway credential serves the inline `+ AI` cell, the **Help** button, and
the Claude terminals. Resolved in order: **⚙ Settings** → the environment →
`.env` (your project's, then the state directory's). Keys typed into Settings
are kept **in memory only** by default; switch to `disk` to persist them to
`settings.json` (mode 600). The key is never sent back to the browser.

An `AWS_BEARER_TOKEN_BEDROCK` already in your environment is left alone, so your
own Claude auth is unaffected. Codex uses the login and configuration from your
installed `codex` CLI; GusNotebook does not copy the gateway key into it. A plain
shell terminal gets only the local notebook access token, with no AI gateway credential.

## What else is in here

**Sessions** group tabs by project — each with its own root, its own standing
instructions for agents, and its own kernels, which keep running when you switch
away. **Skills** are one-directory `SKILL.md` snippets: read by Claude as a
plugin (`/csv-peek`) and by the notebook as a cell to insert. **Environments**
are per notebook — the env button picks the Python, and the choice is recorded in
the `.ipynb` so it survives a restart and still opens in Jupyter.

No `CLAUDE.md` or `AGENTS.md` is ever written into your project. Standing
instructions reach Claude through an app-owned temp file and Codex through a
per-launch developer-instructions override. Codex may ask you to trust the
GusNotebook current-target hook on its first launch; the reviewed script has a
stable path in the app state directory. Claude's temporary files are deleted
when its terminal closes.

## Development

```bash
uv sync --extra test
uv run python -m unittest discover -s tests -p 'test_*.py'
npm ci --ignore-scripts
npm test
npm run build
uv run playwright install chromium
uv run python tests/test_reliability_ui.py
uv run python tests/test_tabs_ui.py
uv run python tests/test_environments_ui.py
uv run python tests/test_appearance_ui.py
uv run python scripts/benchmark.py
```

Browser suites start their own server on an available port with a temporary
project and `GUSNOTEBOOK_HOME`, then stop it and clean up. They never target your
running app. They use Playwright Chromium, with installed Chrome as a fallback
on macOS; `GUSNOTEBOOK_BROWSER_CHANNEL` overrides the choice. Paid LLM calls are
disabled in the disposable harness. `tests/test_new_ui.py` is the extended suite
and additionally exercises installed agent CLIs. The environments suite needs
uv and package index access; it installs packages and a local repository into
temporary environments, then runs a notebook on the new interpreter.

The frontend libraries are pinned in `package-lock.json`. `npm run build`
regenerates `static/vendor/`, including third-party licenses; commit the generated
assets so wheel installs and ordinary launches require neither Node nor CDN
access. CI checks that generated assets match their sources and runs the unit
and offline browser regressions.

`create_app(config)` creates independent app resources, and `close_app(app)`
stops their watchers, terminals, previews, and kernels. Importing the app or
asking for `--help` creates no notebooks or persistent state. Use `WORK_DIR`,
`STATE_DIR`, `NOTEBOOK`, and `START_WATCHERS` in the factory config for tests.
