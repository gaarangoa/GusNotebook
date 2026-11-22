# Analysis projects

This experimental workflow is on `feature/report-first-analysis`. It starts
with an HTML report and keeps its results connected to approved data, Python,
notebooks, a locked environment and review history. Ordinary notebooks, `.gspy`
documents and the HTML editor remain available.

## Start a project

1. Start Docker **on the computer running GusNotebook**. Use Docker Desktop on
   macOS or Docker Engine on Linux. The runner uses a local Docker daemon; a
   remote Docker context is not supported. A tunnel does not move execution
   onto your laptop.
2. Choose **+ → Analysis project**, enter a project name and an existing parent
   directory, and click **Create project**. This creates a new directory; it
   does not overwrite or adopt an existing one.
3. Under **Approved data**, give a CSV or Parquet file an ID and approve its
   absolute path. Paths are on the workspace host. Alternatively, paste or load
   a JSON source list, review it, and choose **Approve listed files**:

   ```json
   {"sources":[{"id":"measurements","path":"/data/measurements.csv"}]}
   ```

   Approval snapshots that exact file, records its SHA-256 and column metadata,
   and does not grant access to its containing directory. Symlinks are rejected;
   supply the real path. Reapproving changed data creates a new revision.
4. Open **Environment → Prepare environment**. The first preparation downloads
   the Python/uv image and scientific packages. Subsequent runs reuse its
   immutable image and `uv.lock`. Preparation needs internet access; analysis
   execution has no network access.
5. Use **Project chat** to draft report sections or request an analysis. It uses
   the existing gateway configured in **Settings**, not a terminal agent login.
   The gateway receives your messages, selected source IDs/column metadata and
   the current analysis code. Input rows and computed results are not included
   automatically. Information you put in messages or code is sent to the model.

The first chat adapter supports bounded turns: it can discuss a request, append
report sections, or write and run one analysis. Follow up to revise it. It does
not have shell, file browsing, approval or acceptance tools.

## Try PCA without a model connection

Choose **Use PCA starter** in the Analysis panel and set parameters such as:

```json
{"source":"measurements","columns":["feature_a","feature_b","feature_c"],"standardize":true}
```

Save the analysis, then click **Run**. The starter validates numeric features,
excludes non-finite/missing rows, reports the exclusion count and uses an actual
scikit-learn `Pipeline` with `StandardScaler` and two-component `PCA`. Constant
features are rejected. The current limit is 3–50,000 complete observations.

Other analyses implement `analyze(data, parameters)`, where `data` is a mapping
of approved source IDs to pandas DataFrames. Return JSON-compatible `title`,
`summary`, scalar `metrics`, an optional scatter `chart`, and/or a `table`.
The source is in `code/src/project_analysis/<section_id>.py`. Add another report
section by selecting **New analysis** and giving it a different ID.

## Review and reproduce

**Preview result** shows a candidate. **Accept into report** updates only that
analysis's marked section and retains surrounding prose. A changed report must
be reloaded before acceptance. Failed runs leave accepted results intact.

Each report result has an **Analysis** link. Click it to inspect its run receipt,
comparison and producing code/notebook downloads. **Open notebook** opens the
current editable notebook; the producing notebook download is the saved
snapshot from the selected run. **Replay saved run** uses the saved source,
parameters, image and exact approved input revisions in a fresh worker. Revoked
or replaced approvals must be explicitly restored before replaying that input.

New runs compare nested JSON values, array lengths and text with the accepted
run. Numeric tolerances are fixed at `rtol=1e-7`, `atol=1e-10`, recorded before
execution and unavailable to the model. Lineage changes are shown separately.
This is numerical/result comparison, not screenshot or scientific validation.
Recorded hashes and versions support reproducibility; they do not guarantee
bitwise equality across machines or make an analysis scientifically sound.

Use **Edit HTML** for source edits or **Open in editor** for GusNotebook's
existing visual HTML tools. Preserve `gusnb` section markers for targeted result
updates. Modifying a generated result marks its content as manually changed.
Source, input or parameter changes mark the accepted analysis as stale.

## Files and execution boundaries

```text
gusproject.json               # portable project description; not an access grant
pyproject.toml, uv.lock       # package definition and locked dependencies
code/src/project_analysis/   # reusable analysis modules and local HTTP adapter
data/inputs/<id>/<hash>.*    # approved input revisions
data/derived/<run>/          # derived JSON results
results/runs/<run>/          # result, receipt, producing notebook, optional wheel
results/comparisons/         # candidate-versus-baseline comparisons
environment/runtime.json   # immutable Docker image and lock identifiers
notebooks/                  # current notebook for each analysis
reports/deck.html           # report with accepted results
reports/views/              # shared D3 view used in notebooks and reports
audit/events.jsonl          # portable history projection
audit/artifacts/            # content-addressed large revisions referenced by events
```

The authoritative registry, input snapshots, run snapshots and history are in
GusNotebook's state directory, outside the project. Editing a project manifest
cannot widen access. Back up both the project and GusNotebook state; copying the
project alone does not import its approvals into another app installation.

The Python worker mounts only its selected input files and source snapshot,
read-only. It has no host home, Docker socket, application token or general
network access. Writable scratch space, CPU, memory, process count, output size
and runtime are bounded. No Docker means no restricted execution; there is no
host-kernel fallback. The project chat controller receives no general terminal
tools. Existing notebook kernels, Claude/Codex terminals and the ordinary HTML
editor retain their existing permissions; they are not this sandbox.

The project preview renders in an opaque sandbox with a restrictive network
policy. Worker output is validated JSON, rendered by bundled D3; workers cannot
write arbitrary report scripts. External images, CDNs and arbitrary asset
fetching are intentionally unavailable in this preview.

The uv environment lives **inside the Linux runtime** at
`/opt/project/environment/.venv`, so a Linux environment is not mistaken for a
macOS interpreter. For an optional regular host notebook environment:

```bash
cd /path/to/project
UV_PROJECT_ENVIRONMENT=environment/.venv uv sync --locked
```

Select `environment/.venv/bin/python` in the notebook. That kernel has ordinary
host permissions. Its executions do not replace the restricted run receipts.

Raw/derived data, results, notebooks, the rendered report, environment and audit
are ignored by the generated `.gitignore` to avoid accidental data publication.
Export or commit sensitive project artifacts deliberately. Source registration
currently allows files up to 256 MB; results up to 8 MB and 50,000 table/chart
rows. Analysis execution is limited to three minutes, 2 CPUs and 2 GB RAM.

## Package and API

**Build package + API** builds a wheel from the selected reusable module in the
container, installs it into a fresh package environment sharing the immutable
locked dependency layer, and reruns it against the same inputs. The result must
match the source analysis. An independent, seeded synthetic dataset with the
same column schema checks basic portability; incompatible schema assumptions
fail the check. This check does not establish general scientific validity.

The verified wheel is written under `results/runs/<run>/`. Install it into an
environment with its declared dependencies. The included loopback HTTP adapter
uses the same `analyze` function:

```bash
python -m project_analysis.api analysis --port 8080
```

POST `/analyze` with `{"data":{"source_id":[{"column":1}]},"parameters":{...}}`.
It accepts records, never arbitrary server file paths. Public deployment,
authentication and production API operations are separate work; this action
does not publish a service.

## History and current limits

The project panel shows recent events and offers **Download full JSONL log**.
Messages, model-context disclosures, code/report revisions, approvals, failures,
run timestamps, comparisons and acceptance decisions persist across restarts.
Large revisions are stored by hash in `audit/artifacts/`; retain that folder
alongside the log for complete reconstruction. This is a reviewable application
record, not a signed or tamper-proof compliance system. It records visible
interactions, not hidden model reasoning or background activity in unrelated
terminals.

This first release creates new projects with a fixed scientific dependency set
and a shared scatter/table/metric renderer. Existing-project import, configurable
dependency updates, automatic multi-step analysis graphs, arbitrary custom D3
modules, arbitrary HTML-region selection in chat, image comparison, full project
export/import, dedicated slide navigation, and sandboxed terminal-agent adapters
remain in the [architecture plan](report-first-analysis.md).
