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

Alternatively, start GusNotebook locally, open **Tunnels**, sign in, click
**Refresh**, and select your tunnel. Use **+** to save a tunnel by its full name.

Keep both processes running. Disconnecting locally leaves the remote workspace
running; stopping GusNotebook on the remote computer shuts it down.
