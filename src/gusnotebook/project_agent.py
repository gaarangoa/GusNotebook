"""Instructions for the existing Claude/Codex terminal, scoped to a project."""

from .project_templates import SLIDE_DECK_DIR


SLIDE_DECK_INSTRUCTIONS = f"""
For a new HTML slide deck or presentation, read and use the html-slide-deck skill:
{SLIDE_DECK_DIR / 'SKILL.md'}
Its reusable HTML template is at {SLIDE_DECK_DIR / 'assets' / 'deck.html'}.
Use it for the deck's design; use the linked notebook for computed figures and tables.
New project reports already start from this template. Keep an existing report's
design when extending it, and honor an explicitly requested alternative design.
"""

WORKSPACE_INSTRUCTIONS = SLIDE_DECK_INSTRUCTIONS + """
For a report or slide deck grounded in data, use the NORMAL GusNotebook workspace:
the existing terminal chat, HTML editor and Jupyter notebook tabs. You can create
the linked files with `gusnb project create NAME /absolute/parent/folder`; it opens
the report and notebook automatically. Or use `gusnb project list` then
`gusnb project open PROJECT_ID`. Read `gusnb project context` for paths and workflow.
Start from the user's HTML and layout. Write and execute the Python data preparation
AND the complete D3/HTML plotting code in the linked notebook, then embed that actual
output into the HTML. Use `gusnb -n NOTEBOOK export-output CELL_ID --file figure.html`.
Do not leave the generating code only in the terminal, an external module or the HTML.
The agent handles this; the user does not need to copy outputs or fill analysis forms.
Use Git/GitHub to version the code and notebooks, including uv dependency files under code/.
Do not create per-run folders, run IDs, a review/acceptance workflow or an analysis API.
"""

INSTRUCTIONS = SLIDE_DECK_INSTRUCTIONS + r'''
This terminal is linked to an ordinary HTML report and Jupyter notebook.
Run `gusnb project context` at the start of each request for the project paths,
approved sources and uv interpreter. Use the existing HTML editor and terminal chat.
New project layout: code/src, code/notebooks, code/tests; data/inputs and
data/environment/.venv; results/reports and results/outputs. Only code/ is tracked
by Git; code/tests/ is ignored. Use paths from context for existing projects.

Default workflow for a plot, table, number or other computed content in the HTML:
1. Read `gusnb here --full` and `gusnb project report` to preserve the requested
   layout and surrounding content. Remember the report revision before editing.
2. Use only the user's authorized inputs. Register exact approved file paths with
   `gusnb project source ID PATH`. Read their snapshots under the project's
   data/inputs/ID/SHA256.SUFFIX (metadata is in context). Save derived data under
   results/outputs/ and reports under results/reports/. If the user explicitly asks
   for dummy/synthetic data, generate it in the notebook with a fixed random seed,
   label it synthetic, and record that choice. Synthetic examples need no external
   source registration. Never substitute dummy data for a real-data request.
3. Prepare the uv environment using `gusnb project prepare` if needed, then poll
   `gusnb project status` until complete. Target the linked notebook explicitly
   with `gusnb -n /absolute/project/code/notebooks/analysis.ipynb ...`. Check its kernel
   with `env`; select the project environment.python if it differs. Do not switch
   an already correct kernel. uv manages dependencies; do not create a service.
4. Put readable Python data preparation and ALL custom plotting code in ordinary
   notebook cells. Use `add -` to write/run a cell or `set CELL_ID - --run` to edit
   it with history. For D3, use display(HTML(...)) with the actual inline D3 script
   and JSON data computed by Python. Bundled D3 is available in notebook outputs;
   follow the normal D3 instructions. Any chart type and custom layout are allowed.
   Show the full plotting code in the notebook, not just an import, result JSON,
   a link to a .py file or code that reads a hidden renderer. Keep calculations in
   Python and the D3 rendering beside them. Put explanatory Markdown between cells.
5. After successful execution, export the producing cell:
   `gusnb -n NOTEBOOK export-output CELL_ID --file /absolute/project/results/outputs/figure.html`.
   Use --output N for a particular displayed output (zero based). This writes a
   self-contained HTML fragment with the actual output, code snapshot and history.
   Read it from the file programmatically and insert it into the requested position
   in the report. Keep its provenance attributes and Notebook code button. Do not
   recreate the chart independently in the report, change the exported data, or
   print the bundled D3 library into the conversation.
6. Save the updated HTML with `gusnb project report --file updated.html --revision SAVED_REVISION`.
   Preserve surrounding HTML and existing gusnb markers. If revision conflicts,
   reread and merge the user's changes. Verify the notebook and report both show
   the result. To change a figure, edit/rerun its cell, re-export and replace only
   that figure. Report figures are saved snapshots; rerunning alone does not
   silently replace a reviewed report. Source-only setup cells need no export.
7. Keep explanations, data choices and limitations in notebook Markdown. Cell history
   already keeps prompts, edits, executions and output links inside the notebook.
   Version changes with ordinary Git commits. Inspect git status/diff; stage only
   code/ for this task (including notebooks and uv definitions), excluding code/tests/, then
   commit with a meaningful message. Follow the user's commit/push preferences.
   Reuse the existing GitHub remote and credentials. If no remote is configured,
   keep commits local and ask for the intended GitHub repository before connecting
   or pushing. Do not create a remote repository or invent a Git identity.
   Never stage code/tests/, data/, results/, secrets or unrelated files. Reports and outputs stay local.

Keep the normal workspace and the user's chosen HTML design. Do not force arbitrary
D3 plots through `project save/run/publish` or its scatter/metric/table schema.
Do not generate separate run receipts, run IDs or copies of notebooks per execution.
Git commits are the version history; use git log/diff or GitHub to inspect changes.
Never overwrite a hand-authored notebook with a generated structured analysis.
Reusable Python modules and sklearn Pipelines can be factored out when requested,
but the notebook must still show how its displayed content is generated.
Do not create api.py or an HTTP service for analysis. Do not use Docker.
Respect approved data paths; uv is dependency isolation, not a filesystem sandbox.
Never accept results on the user's behalf or invent numerical results.
'''
