# Reports backed by executable notebooks

Development branch: `feature/report-first-analysis`.

The normal terminal agent edits an HTML report or slide deck. Its supporting
Python analysis and complete D3 rendering code live in ordinary Jupyter cells.
The agent runs those cells and embeds their actual outputs into the requested
HTML layout. A figure links back to its generating cell.

Projects use three folders: `code/`, `data/`, and `results/`. Python sources,
notebooks, tests and uv dependency definitions live under `code/`; environments
live under `data/environment/`; reports and outputs live under `results/`.
Git tracks `code/` except `code/tests/`. Data, environments and results are ignored.

Git/GitHub provide version history. Notebook cell metadata preserves prompts,
edits, executions and output provenance. There is no separate run-ID/review
workflow in the normal interface, and no generated analysis API service.
Existing run artifacts remain readable for compatibility.

The HTML editor, notebook kernel picker, terminal placement and existing Git
panel remain the workspace. See [the workflow](analysis-projects.md) for commands
and the exact layout. uv handles dependencies; it is not a filesystem sandbox.
