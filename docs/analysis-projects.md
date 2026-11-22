# Linked reports and notebooks

On `feature/report-first-analysis`, work in the normal GusNotebook workspace:
**Claude/Codex terminal chat on the left, HTML editor and notebook tabs on the
right**. Ask the agent to create a report or slide deck and the analysis behind
it. The agent manages the files and uv environment through the existing terminal.

## Start from the chat

Open your usual Claude or Codex terminal and describe the report, the new project
folder, and the exact data files it may use. For example:

> Create a slide deck in /home/me/projects/cohort_study. You may use
> /home/me/data/measurements.csv. Show PCA using feature_a, feature_b and feature_c,
> explain the results, and link the generating Jupyter notebook.

The agent creates the project, opens `reports/deck.html` and
`notebooks/analysis.ipynb` as normal tabs, registers the authorized inputs,
prepares uv, and saves/runs the analysis. Computed results appear in the report
with notebook links. You can also use **+ → Analysis project** to create or open
a project with a name and parent folder, then continue in the chat.

Use the ordinary HTML editor for visual or source edits, zoom, images and text.
Click **Analysis** beneath a result to open the notebook from that specific run.
The compact **Notebook** button opens the current editable analysis notebook,
with its actual Python code, saved outputs and project interpreter selected.
The **Report** button returns to the document. Notebook execution remains
separate from recorded project runs: ask the agent to update and run the analysis
to publish a new result to the report.

uv must be installed on the computer running GusNotebook. It creates
`environment/.venv`, downloads Python 3.12 and packages when needed, and saves
`uv.lock`. Through a tunnel, these operations happen on the remote computer.
New project terminals activate the prepared environment; existing terminals
keep running, and project commands use the correct interpreter regardless of
when that terminal started. No Docker or separate model gateway is required.

## Review and history

Publishing a result displays a **draft**. **Review result → Accept result** records
your acceptance as the comparison baseline. Publishing later drafts does not
change that baseline. Failed runs leave the displayed report intact. Report
updates replace only the marked analysis section, preserving surrounding prose;
keep the `gusnb` markers when editing HTML source.

**History** shows approved inputs, runs, comparisons, environment information,
user prompts and recorded agent explanations. Saved runs include their producing
notebook, receipt and a **Replay** action. Full history is downloadable as JSONL.
History persists in app state and `audit/events.jsonl`; large revisions are saved
by hash in `audit/artifacts/`. Back up app state and the project together.

Prompt hooks record user requests when enabled by the terminal provider. Agents
are instructed to record their explanations with `gusnb project note`; complete
terminal output is not automatically transcribed. Project commands record
code/report revisions, inputs, versions, runs, errors and acceptance. Direct
filesystem edits bypass those project revision events. These are review records,
not a signed compliance system.

## Agent commands

The terminal discovers its current project from the server, so creating or
opening a project does not require restarting the agent.

```bash
gusnb project create cohort_study /home/me/projects
gusnb project list
gusnb project open PROJECT_ID
gusnb project context
gusnb project source measurements /home/me/data/measurements.csv
gusnb project prepare
gusnb project save analysis analysis.json
gusnb project run analysis
gusnb project status
gusnb project result RUN_ID
gusnb project publish RUN_ID
gusnb project report
gusnb project report --file updated.html --revision SAVED_REVISION
gusnb project note "Explained exclusions and limitations."
gusnb project run analysis --replay RUN_ID
gusnb project run analysis --package
```

Analysis JSON contains `code`, `parameters`, `source_ids` and `revision`. Use the
existing `code_revision` from context, or `"new"` for a new analysis. Use `-` for
stdin with `save`, `note` or `report --file`. Revisions prevent overwriting edits
made while the agent works. Register only data paths explicitly authorized by
the user. `publish` displays a draft; acceptance is a human review action.

Analyses define `analyze(data, parameters)`, where `data` maps approved CSV or
Parquet source IDs to pandas DataFrames. Return JSON-compatible `title`,
`summary`, scalar `metrics`, an optional scatter `chart`, and/or a `table`.
Python computes the results; bundled D3 renders them in HTML and the notebook.
The PCA starter uses a scikit-learn Pipeline, validates features and reports
exclusions. Other analyses can implement the same reusable interface.

## Reproduce and package

Each approved input is copied and hashed. Symlinks are rejected; use the real
path. Changed files need a new approval. Replay uses saved code, parameters,
lock and exact approved input revisions. Revoked or replaced inputs cannot be
replayed until their original revision is approved again. Older Docker receipts
remain readable; prepare uv and create a new baseline to continue those projects.

Comparisons use `rtol=1e-7`, `atol=1e-10`, plus structural/text checks and lineage
changes. These support reproducibility without establishing scientific validity
or guaranteeing bitwise equality across machines. The `--package` command builds
a wheel and local API, then checks the installed package against the source
result and an independent same-schema synthetic fixture.

```text
pyproject.toml, uv.lock      # dependencies and lock
code/src/project_analysis/  # reusable modules and HTTP adapter
data/inputs/<id>/<hash>.*   # approved file snapshots
data/derived/<run>/         # computed JSON
results/runs/<run>/         # result, receipt, producing notebook, optional wheel
results/comparisons/        # candidate-versus-baseline comparison
environment/.venv/          # uv Python and packages
environment/runtime.json   # interpreter and lock metadata
notebooks/                 # editable analysis notebooks
reports/deck.html          # ordinary HTML report
audit/                     # events and saved revisions
```

The local API runs with `python -m project_analysis.api analysis --port 8080`.
POST `/analyze` with `{"data":{"source_id":[{"column":1}]},"parameters":{...}}`.
Package generation does not deploy or publish a service.

## Current limits

uv isolates dependencies, **not filesystem or network access**. Agents and Python
run with the host user's permissions. Approved sources govern project loading
and provenance; agent instructions alone cannot enforce access to those files
only. Provider permissions and model data handling follow your terminal setup.

The first release has a fixed scientific dependency set and shared
scatter/table/metric renderer. Importing unregistered projects, dependency
editing, arbitrary D3 modules and full project export remain future work.
Sources are limited to 256 MB, results to 8 MB/50,000 rows, worker output to 10 MB
and execution to three minutes. Cancellation stops only the worker process
group; notebook kernels and terminals keep running. uv does not impose memory
or CPU limits. Generated data, reports, environments and logs are gitignored.
