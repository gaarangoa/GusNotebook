# Reports backed by notebooks

Work in the normal GusNotebook workspace: HTML and notebook tabs in the center,
Claude/Codex terminal chat on the right. Create or open a project with
**+ → Analysis project**, then use the usual **Agent → Claude or Codex** control
while browsing its folder. Describe the report or slide you want. The agent receives the project's
data, environment, folder, Git, and slide-deck instructions. It runs the analysis
through notebooks, importing reusable Python modules when appropriate, then
embeds the actual output into your HTML layout.

For example:

> Create a slide deck in /home/me/projects/cohort_study. Use only
> /home/me/data/measurements.csv. Add a D3 boxplot on the left, with an explanation
> on the right. Keep the data preparation and plotting code in the notebook.

Select a figure, then choose **Source → Open notebook code** to open its generating cell. The project
bar has **Report**, **Notebook**, **Data**, and **Inputs**. Keep using the ordinary HTML editor
for layout, text, images, zoom and source edits. Explicit dummy-data requests
use a recorded random seed and need no external data-file registration.

New projects start with the [HTML slide-deck template](../src/gusnotebook/bundled_skills/html-slide-deck/assets/deck.html),
adapted from the user's model-architecture presentation: white slides, blue
headings, compact cards, workflow diagrams, and results tables. The five sample
layouts can be removed, duplicated, or rearranged. They contain placeholders,
not example scientific results. Slides adapt to narrow screens and support printing.
Project agents are directed to read the bundled
[slide-deck skill](../src/gusnotebook/bundled_skills/html-slide-deck/SKILL.md)
when creating presentations. Existing reports keep their design.

There is one Agent control. The terminal's working directory determines its
project: registered project folders and their subfolders receive project rules,
and the closest project wins if projects are nested. Outside those folders,
agents use the regular notebook/HTML workflow. Opening a file or changing browser
tabs does not change a terminal's project or give it unrelated focused content.
The prompt hook and `gusnb` commands refresh context when the terminal changes
directory; the context includes the default notebook, available named notebooks,
approved paths and uv interpreter.

An existing terminal can explicitly join or switch projects with
`gusnb project create NAME /parent/folder` or `gusnb project open PROJECT_ID`.
The command selects that project and returns its instructions even when run
from a parent directory. This selection lasts until the terminal changes directory.
Existing conversations retain earlier messages, but project instructions are
scoped to the current context. Open a fresh conversation to discard old context entirely.

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
  access.yaml             # manually approved input files/directories
  processed/              # derived datasets and cohorts
  README.md               # data descriptions and producing code links
  lineage.json            # portable machine-readable data catalog
  environment/.venv/      # uv environment
results/
  reports/                # HTML reports and slides
  outputs/                # figures, tables and other generated outputs
```

Git tracks **only `code/`, excluding `code/tests/`**, plus the root `.gitignore`
that enforces this rule. Data, environments, reports and generated outputs remain
local. Operational metadata lives under `data/.gusnotebook/`. Existing projects
keep their paths so their open notebooks and kernels continue working; agent
context supplies the actual paths, including older top-level `notebooks/` and
dependency files when describing what to commit.

uv must be installed where GusNotebook runs. The agent prepares
`data/environment/.venv` and selects it as the notebook kernel. Dependency
files live in `code/`, so Git versions them with the analysis. Through a tunnel,
execution and files remain on the remote computer.

## Git and GitHub

New projects initialize Git when needed. Open **Git** in the left sidebar to
review diffs, stage files, commit and push. Commit history replaces
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

### Choose notebooks and Python modules

Project agents choose by scientific purpose:

- Continue the same question in its existing notebook, including additional
  figures, revisions and reruns. Follow the producing notebook/cell when changing
  an existing report figure.
- Create a descriptive notebook for a separate question, such as
  `code/notebooks/cohort_characterization.ipynb` or `survival_analysis.ipynb`.
  The initial `analysis.ipynb` is a starting point. There are no per-prompt,
  per-run or timestamped notebook copies, and existing work is not reorganized
  just to follow this policy.
- Put reusable transformations, shared calculations and model components in
  `code/src/project_analysis/*.py`. Import and call them from notebook cells.
  Short, analysis-specific exploration can stay in the notebook. Modules take
  explicit inputs/parameters, return results, and do not load data or run analysis
  on import. sklearn estimators/Pipelines are preferred for reusable modeling.

Keep each notebook's question, approved inputs, parameters/seeds, explanations,
module links and calls, figure-specific plotting code, outputs and interpretation
visible. Maintain an index of notebook purposes and module links in `code/README.md`.
These are project-specific agent instructions, not a rule that automatically
splits files or moves existing code. Ordinary notebooks outside projects keep
their current behavior.

`gusnb project context` lists notebooks directly inside the project's notebook
folder without reading their contents. The project's **Notebook** button offers
these notebooks when more than one exists. `context.notebook` remains the default;
always name the chosen target explicitly:

```bash
gusnb open /path/to/project/code/notebooks/survival_analysis.ipynb
gusnb -n /path/to/project/code/notebooks/survival_analysis.ipynb env /path/to/project/data/environment/.venv/bin/python
gusnb -n /path/to/project/code/notebooks/survival_analysis.ipynb add - < analysis_cell.py
```

Use `layout_paths.notebooks` from context for older layouts. Set up module imports
from `code/src` in a reproducible notebook setup cell, or install the project in
editable mode into the same uv environment. After a module changes, reload it or
restart the chosen idle kernel, rerun affected cells and re-export affected report
outputs. A saved cell snapshot does not include imported module implementations,
and the exporter does not automatically detect changes to them. Git versions
modules and notebooks together; data records identify the actual producing
notebook/cell, with module paths described in the transformation. Direct terminal
script runs can be registered as producers but do not receive observed cell history.

### Choose approved inputs

Open **Inputs** and edit `data/access.yaml` yourself before asking the agent to
use real data. New and upgraded projects start with empty lists. Existing source
registrations do not automatically approve inputs. For example:

```yaml
version: 1
files:
  - /home/me/data/measurements.csv
directories:
  - /home/me/data/approved_study
```

Paths refer to the computer running the workspace. Relative paths resolve from
the project root. An approved directory includes its descendants; files approve
only that exact file. Wildcards and symlinks are unsupported. Keep `files: []` or
`directories: []` when that list is empty. No data is copied or scanned when the
policy is read.

Agents receive the current policy with project context on each request. They
must use only these raw inputs, must not change the YAML, and must ask you to
update it when data is missing. Mentioning a path in chat or registering it with
`project source` does not grant access. Project code, notebooks, the uv environment
and generated outputs remain available for ordinary project work.

```bash
gusnb project inputs                         # approved files/directories
gusnb project inputs /home/me/data/approved_study  # one directory level
gusnb project inputs /home/me/data/approved_study --offset 100 --limit 100
```

Use the listing's `next_offset` for another page. Discovery is limited to approved
paths, returns file metadata without reading contents, and skips symlinks. The
app checks the policy before source registration, processed-data lineage using
raw inputs, and structured analysis runs. Invalid or missing YAML denies those
operations. Removing a grant hides that source from current project context and
blocks its use in these operations; historical records remain intact. Policy
changes observed during project refresh are saved in project activity history.

**Enforcement boundary:** these are agent instructions and checks in the listed
GusNotebook operations. Direct terminal commands, arbitrary notebook/Python code,
general file APIs and network tools retain their existing host permissions. The
YAML is editable by processes running as your user; it is not proof of who
approved a path. This feature does not provide filesystem isolation. Ordinary
notebooks outside registered projects keep their existing behavior.

### Register and use data

```bash
gusnb project create cohort_study /home/me/projects
gusnb project context
# Edit data/access.yaml yourself before registering this input.
gusnb project source /home/me/data/measurements.csv --description "Original study measurements"
gusnb project prepare
gusnb project status
```

Data stays at the original path, even outside the project. Registration saves
the path, size, file timestamps and available column metadata. It does not copy
the dataset, rename it, hash its contents, or impose a file-size limit. CSV
inspection reads at most 1 MB; Parquet inspection reads metadata. Other regular
files can also be registered and read with the appropriate notebook library.
The agent should select columns/partitions or process chunks for large inputs.
Save derived datasets and cohorts in `data/processed/`; figures and presentation
outputs belong in `results/outputs/`.

Registering the same path reuses its entry. The optional form
`gusnb project source LABEL /original/path` supplies a readable label, useful
for files with the same filename or for relinking an existing entry. Labels
are metadata; they do not create new datasets or directories. The original
file must remain available on the computer running the project. Size and
timestamp checks detect ordinary changes; they are not a content checksum or
an archived version of the data. The older structured run/replay commands reject
changed inputs until you register the path again; replay also requires the
recorded file revision. Ordinary notebook code reads the file currently at its path.

Registrations made by older versions appear as `legacy_snapshot`: those versions
discarded the original path. Relink one with its existing label and original
path. Old copies and existing notebook code are preserved so older analyses
continue to work; update those notebooks to read the original path before
removing any old copies.

### Record processed data

After generating or updating a dataset, the project agent records what it
contains, its inputs, the transformation, parameters, and the producing notebook
cell or Python file. These requirements apply only to analysis projects.
They do not change the ordinary notebook workflow outside project folders.

For example, after running the cell that writes `data/processed/cohort.csv`:

```bash
gusnb project data --file - <<'JSON'
{
  "path": "data/processed/cohort.csv",
  "inputs": ["measurements"],
  "description": "Adult participants with measured outcomes",
  "transformation": "Keep participants aged at least 18",
  "parameters": {"minimum_age": 18},
  "producer": {
    "path": "code/notebooks/analysis.ipynb",
    "cell_id": "REPLACE_WITH_PRODUCING_CELL_ID"
  }
}
JSON

# Show the current data catalog
gusnb project data
```

Paths in this metadata are relative to the project. Replace the example cell ID
with the ID returned by `gusnb add` or shown by `gusnb list`. For a Python script,
provide `producer.path` without `cell_id`. Inputs can be registered raw labels,
original paths, or previously recorded processed-dataset paths. Record upstream
datasets first. Empty inputs require an explicit `"synthetic": true` declaration
for user-requested synthetic data.

The **Data** button opens `data/README.md`. GusNotebook maintains its marked
catalog section and preserves your notes outside it. `data/lineage.json` holds
the current records; `data/.gusnotebook/audit/events.jsonl` retains dataset
revisions and project activity, including captured user prompts and agent notes.
Notebook producers also receive a dataset entry in their cell history. Recording
an updated dataset replaces its current catalog entry while retaining earlier
metadata in the history. It does not make extra data copies or run folders.

Records include input file revisions, code fingerprints, available environment
and Git information, and the agent/session/request context when available.
Descriptions and input relationships are declared by the author or agent.
Matching successful notebook executions are identified when GusNotebook observed
them; they are not independent proof that a particular process wrote a file.
External edits must be re-recorded. File revision checks use metadata, not full
data hashes, and cannot recreate overwritten data. Activity history covers
captured actions, not time spent or work performed outside GusNotebook.

The catalog and activity files stay local under the existing data ignore rule;
cell history travels with the Git-tracked notebook. JSON and JSONL provide a
portable foundation for future tracking integrations without another service.

### Generate report outputs

Once preparation finishes, choose the notebook using the policy above. Keep the
analysis-specific Python, calls to reusable modules and D3 `display(HTML(...))`
code there:

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
