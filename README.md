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

## Where things live

Two directories, deliberately separate:

| | |
|---|---|
| **Your project** | wherever you ran `gusnotebook` — notebooks, data, `.env` |
| **App state** | `~/.config/gusnotebook/` — `settings.json`, `sessions.json`, `skills/` |

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

## Credentials

One gateway credential serves the inline `+ AI` cell, the **Help** button, and
the Claude terminals. Resolved in order: **⚙ Settings** → the environment →
`.env` (your project's, then the state directory's). Keys typed into Settings
are kept **in memory only** by default; switch to `disk` to persist them to
`settings.json` (mode 600). The key is never sent back to the browser.

An `AWS_BEARER_TOKEN_BEDROCK` already in your environment is left alone, so your
own Claude auth is unaffected. Codex uses the login and configuration from your
installed `codex` CLI; GusNotebook does not copy the gateway key into it. A plain
shell terminal gets no credential at all.

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
uv run gusnotebook &
uv run python tests/test_tabs_ui.py
uv run python tests/test_new_ui.py       # NO_LLM=1 skips the one paid test
```

Both suites drive a real browser against a running app, and both point
`GUSNOTEBOOK_HOME` at a temp directory so they can't damage your settings.
