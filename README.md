# GusNotebook

**Traceable data science, from exploration to communication.**

GusNotebook connects analysis, AI collaboration, and scientific communication
in one workspace. Jupyter-compatible notebooks, AI agents, and visual document
editing support the analysis lifecycle, with human review and scientific
judgment at the center.

Track agent requests, code changes, executions, and analyst notes at the cell
level. Bring notebook outputs—including tables and visualizations—directly into
web pages and HTML presentations, using provenance snapshots to keep shared
results connected to the analysis that produced them.

Designed for everyday research in biopharma and other fields where traceability
matters, GusNotebook helps you review agent contributions, document analytical
decisions, and communicate findings with a clear record of how they evolved.

Runs on **macOS and Linux (including WSL)**, locally or on a remote computer.
Agent CLIs are optional; install and sign in to them where the workspace runs.

## Install with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed,
then open a new terminal:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install GusNotebook directly from GitHub (requires Git):

```bash
uv tool install 'git+https://github.com/gaarangoa/GusNotebook.git@main'
uv tool update-shell
```

Open a new terminal and launch it from your project directory:

```bash
cd /path/to/your/project
gusnotebook --port 4477
```

The browser opens your workspace. The default port is 8888; the example uses
4477 if 8888 is occupied. No environment activation or frontend build is needed.

To install into system Python instead, use:

```bash
uv pip install --system 'git+https://github.com/gaarangoa/GusNotebook.git@main'
```

HTML documents (`.html` and `.htm`) have no file-size limit for opening, visual
editing, previewing, or saving, so reports can include large embedded figures.
PDFs also have no file-size cap and are served to the browser viewer with
byte-range support, without loading them into a text editor.

## Table previews

Open `.csv`, `.tsv`, `.parquet`, `.feather`, or
`.xlsx` files from the file browser to see a table. Search the preview, click a
column heading to sort, or use Previous/Next to page through 50 rows at a time.
Delimited files offer a delimiter selector and a header-row toggle; Excel also
offers a sheet selector. CSV and TSV retain a **Source** view for
editing. Parquet, Feather, and Excel previews are read-only. Excel formulas show
their last saved values and are not recalculated.

Files larger than 2 MB are refused without loading their contents. Smaller files
show at most 5,000 rows, 100 columns, and 100,000 cells, with long values shortened
and an additional preview text budget. Compressed binary tables that expand past
32 MB are also refused. A notice identifies partial snapshots; search and sorting
apply only to the preview. These actions do not change the file or start a kernel.
Use **Reload** to refresh binary snapshots after a file changes on disk.

JSON files open as formatted, collapsible JSON with an editable **Source** view.
Large branches show 100 entries at a time. Invalid or overly complex JSON falls
back to plain text. `.jsonl` and `.ndjson` files also open as plain text.
The same 2 MB file limit applies.

## Tunnels: installation and setup

Install GusNotebook and Microsoft's
[devtunnel CLI](https://learn.microsoft.com/azure/developer/dev-tunnels/get-started#install)
on **both computers**. uv does not install devtunnel.

**macOS:**

```bash
brew install --cask devtunnel
```

**Linux / WSL:**

```bash
curl -fsSL https://aka.ms/DevTunnelCliInstall | bash
export PATH="$HOME/bin:$PATH"
```

**On the remote computer**, start in your project directory:

```bash
cd /path/to/your/project
gusnotebook --tunnel my-research --tunnel-login github --port 4477
```

Follow the displayed login URL and device code. Use the **same account on both
computers**; replace `github` with `microsoft` in both commands for Microsoft login.

**On your local computer**, connect:

```bash
gusnotebook --connect my-research --tunnel-login github
```

Open the local browser address printed by GusNotebook. Files, kernels, and agents
run remotely, with no additional notebook token or password. If needed, use the
full tunnel name printed by the remote computer.

Alternatively, start GusNotebook locally and open **Accounts** at the bottom of
the left bar. **Sign in with GitHub** connects Git and tunnels in one guided
workflow, reusing existing sign-ins. First-time setup requires separate approvals
for GitHub CLI and Dev Tunnels. Microsoft sign-in for tunnels is also available
under **Other sign-in options**. Then open **Tunnels**, click **Refresh**, and
select your tunnel. Use **+** to save a tunnel by its full name.

If either CLI is missing, Accounts offers **Set up GitHub** on macOS and Linux
(Intel or ARM64). It downloads the official tools into GusNotebook's per-user
state directory, shows progress, and continues to sign-in. No administrator
password or Homebrew is needed. Existing installations are reused; a failed or
canceled download can be retried from the same popup.
Linux may also require the distribution's `libsecret` package for tunnel credential storage.

Keep both processes running. Disconnecting locally leaves the remote workspace
running; stopping GusNotebook on the remote computer shuts it down.
