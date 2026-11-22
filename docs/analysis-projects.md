# Reports backed by notebooks

Work in the normal GusNotebook workspace: HTML and notebook tabs in the center,
Claude/Codex terminal chat on the right. Describe the report or slide you want.
The agent writes and runs its Python analysis and complete D3 plotting code in
the linked notebook, then embeds the actual output into your HTML layout.

For example:

> Create a slide deck in /home/me/projects/cohort_study. Use only
> /home/me/data/measurements.csv. Add a D3 boxplot on the left, with an explanation
> on the right. Keep the data preparation and plotting code in the notebook.

Click **Notebook code** below a figure to open its generating cell. The project
bar has **Report**, **Notebook**, and **Git**. Keep using the ordinary HTML editor
for layout, text, images, zoom and source edits. Explicit dummy-data requests
use a recorded random seed and need no external data-file registration.

## Project layout

```text
code/
  src/                    # Python code
  notebooks/              # executable notebooks with Python and D3
  tests/                  # tests, excluded from Git
  pyproject.toml          # dependency definitions
  uv.lock                 # locked dependency versions
  .python-version
data/
  inputs/                 # authorized input snapshots
  environment/.venv/      # uv environment
results/
  reports/                # HTML reports and slides
  outputs/                # figures, tables and other generated outputs
```

Git tracks **only `code/`, excluding `code/tests/`**, plus the root `.gitignore`
that enforces this rule. Data, environments, reports and generated outputs remain
local. Operational metadata lives under `data/.gusnotebook/`. Existing projects
keep their paths so their open notebooks and kernels continue working; agent
context supplies the actual paths.

uv must be installed where GusNotebook runs. The agent prepares
`data/environment/.venv` and selects it as the notebook kernel. Dependency
files live in `code/`, so Git versions them with the analysis. Through a tunnel,
execution and files remain on the remote computer.

## Git and GitHub

New projects initialize Git when needed. The **Git** button opens the existing
Git panel to review diffs, stage files, commit and push. Commit history replaces
the separate run-ID/review workflow. Cell history stays inside the notebook and
therefore travels with its Git versions.

Use your existing GitHub remote and credentials. If this is a new repository,
configure its intended GitHub URL once in the terminal:

```bash
cd /path/to/project
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git add .gitignore code
git commit -m "Add analysis notebook"
git push -u origin HEAD
```

Git needs your configured author name/email and access to that repository.
The app uses the existing host credentials or Git sharing through your tunnel.
It does not create a GitHub repository automatically.

## Agent workflow

```bash
gusnb project create cohort_study /home/me/projects
gusnb project context
gusnb project source measurements /home/me/data/measurements.csv
gusnb project prepare
gusnb project status
```

Once preparation finishes, use the notebook path from context. Write both the
Python data preparation and the D3 `display(HTML(...))` code there:

```bash
gusnb -n /path/to/project/code/notebooks/analysis.ipynb add - < plot_cell.py
gusnb -n /path/to/project/code/notebooks/analysis.ipynb export-output CELL_ID --file /path/to/project/results/outputs/figure.html
```

Insert the exported HTML fragment into the requested part of the report. It
contains the executed output, bundled D3, a code snapshot and its notebook link.
It works offline. Use `--output N` for a particular displayed output, starting
at zero. Exports currently use `.ipynb` notebooks and reject stale or failed
cell outputs. Keep the source notebook open to use its code link.

Read the report and its revision before changing it, then save with that revision:

```bash
gusnb project report
gusnb project report --file updated.html --revision SAVED_REVISION
```

To update a plot, edit and rerun its cell, then re-export that output into the
HTML. Rerunning alone does not replace an existing report. Put explanations and
data choices in notebook Markdown, and commit the code/notebook changes with Git.
No per-run notebook copies or analysis API service are needed.

uv isolates dependencies, not filesystem access. Approved input instructions
alone cannot restrict an agent or Python process to particular host files.
