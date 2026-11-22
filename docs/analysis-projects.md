# Analysis projects

On `feature/report-first-analysis`, **+ → Analysis project** starts an HTML
report backed by Python analyses, approved input snapshots, notebooks and a
reviewable history. It uses the existing **Claude/Codex terminal agent** and
**uv**. Ordinary notebooks, `.gspy` documents and the HTML editor remain available.

## Start a project

1. Install uv on the computer running GusNotebook. Through a tunnel, execution
   still happens on that remote computer.
2. Choose **+ → Analysis project**, enter a name and existing parent directory,
   then **Create project**. This creates a new folder.
3. Under **Approved data**, register CSV or Parquet files with an ID and an
   absolute path on the workspace host. You can also load a JSON source list:

   ```json
   {"sources":[{"id":"measurements","path":"/data/measurements.csv"}]}
   ```

   Review the list before approving. Each file is copied and hashed. Symlinks
   are rejected; use the real path. Changed files need a new approval.
4. Choose **Environment → Prepare environment**. uv creates
   `environment/.venv`, installs the scientific dependencies and saves `uv.lock`.
   Python 3.12 and packages are downloaded when needed. Docker is not required.
5. In the terminal pane, choose **Claude** or **Codex**, then **Agent**. It uses
   the same CLI, login and terminal UI as the rest of GusNotebook, with the
   project as its working directory and the prepared environment activated.
   **Terminal** opens a shell in the same environment. Closing the project view
   keeps terminal sessions running; reopen it to resume.

Ask the agent to draft the report and analyze your approved inputs. It receives
project instructions and uses `gusnb project` commands to save code, run Python,
inspect results and update HTML. There is no separate model gateway to configure.
If you prepare the environment after opening a terminal, open a new terminal to
activate it there; project runs already use the prepared interpreter.

uv isolates dependencies, **not filesystem or network access**. Terminal agents
and analysis code run with the host user's permissions. Approved inputs govern
the broker's data loading and provenance; agent instructions require those inputs
but cannot enforce approved-file-only access. Use trusted code and your provider's
permissions. Model data handling follows your terminal provider's configuration.

## Review and reproduce

Analyses define `analyze(data, parameters)`, where `data` maps approved source IDs
to pandas DataFrames. Return JSON-compatible `title`, `summary`, scalar `metrics`,
an optional scatter `chart`, and/or a `table`. The bundled PCA starter uses a real
scikit-learn `Pipeline`, validates numeric columns and reports excluded rows.

To try it without an agent, choose **Use PCA starter**, enter parameters such as:

```json
{"source":"measurements","columns":["feature_a","feature_b"],"standardize":true}
```

Save and **Run**, then **Preview result**. **Accept into report** updates only
that analysis's marked section, preserving surrounding prose. Failed runs keep
accepted results intact. Clicking **Analysis** in a report result opens its
receipt, producing source and notebook. **Open notebook** opens the current
editable notebook; select `environment/.venv/bin/python` as its kernel.

**Replay saved run** uses saved code, parameters, lock and approved input
revisions. uv reconciles the environment with the lock before execution.
Replaced/revoked inputs cannot be replayed until their exact revision is approved
again. Older Docker-based receipts remain readable but cannot be replayed by uv;
prepare a uv environment and create a new baseline.

Results are compared with the accepted run using `rtol=1e-7`, `atol=1e-10`, plus
structural/text comparisons. Lineage changes are shown separately. These checks
support reproducibility; they do not establish scientific validity or guarantee
bitwise equality across machines. **Edit HTML** and **Open in editor** retain the
existing editing workflow. Preserve `gusnb` markers for targeted result updates.

## Terminal commands and history

Project terminals receive `NB_PROJECT` automatically. Useful commands:

```bash
gusnb project context
gusnb project prepare
gusnb project save pca analysis.json  # code, parameters, source_ids, revision
gusnb project run pca
gusnb project status
gusnb project result RUN_ID
gusnb project report
gusnb project report --file updated.html --revision SAVED_REVISION
gusnb project note "Explained missing-value exclusions and limitations."
gusnb project run pca --replay RUN_ID
gusnb project run pca --package
```

Use `-` for stdin with `save`, `note` or `report --file`. Save JSON needs the
existing `code_revision`, or `"new"` for a new analysis. Optimistic revisions
prevent overwriting edits made while the agent works. Source approvals and result
acceptance are UI actions, not agent CLI commands.

Prompt hooks record user requests when enabled by the provider. The agent is
instructed to save its responses/decisions using `note`; complete terminal output
is not automatically transcribed. Broker operations record code/report revisions,
inputs, versions, runs, errors, comparisons and acceptance. Direct filesystem edits
bypass those revision events, so use the project commands for auditable changes.

History persists in app state and `audit/events.jsonl`; large revisions are stored
by hash in `audit/artifacts/`. **Download full JSONL log** exports the events.
Back up app state and the project together. These are review records, not a
signed or tamper-proof compliance system.

## Files and packages

```text
pyproject.toml, uv.lock      # project dependencies and lock
code/src/project_analysis/  # reusable modules and HTTP adapter
data/inputs/<id>/<hash>.*   # approved file snapshots
data/derived/<run>/         # computed JSON
results/runs/<run>/         # result, receipt, producing notebook, optional wheel
results/comparisons/        # candidate-versus-baseline comparison
environment/.venv/          # uv-managed Python and packages
environment/runtime.json   # interpreter and lock metadata
notebooks/                 # editable analysis notebooks
reports/deck.html          # HTML report with accepted results
audit/                     # events and saved revisions
```

**Build package + API** builds a wheel, installs it in a fresh package environment
sharing the locked dependencies, compares its result with the source analysis,
and checks an independent same-schema synthetic fixture. The included local API
uses `python -m project_analysis.api analysis --port 8080`; POST `/analyze` with
`{"data":{"source_id":[{"column":1}]},"parameters":{...}}`. This does not deploy
or publish a service.

The preview uses sandboxed HTML and bundled D3, with no external asset fetching.
Source files are limited to 256 MB, results to 8 MB/50,000 rows, worker output to
10 MB and execution to three minutes. Cancelling a run stops its process group;
notebook kernels and agent terminals keep running. uv does not impose memory or
CPU limits. Generated data, reports, environments and logs are gitignored.

This first release has a fixed scientific dependency set and shared
scatter/table/metric renderer. Existing-project import, dependency editing,
arbitrary D3 modules and full project export remain future work.
