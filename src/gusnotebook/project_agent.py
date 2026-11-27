"""Instructions for the existing Claude/Codex terminal, scoped to a project."""

from .project_templates import SLIDE_DECK_DIR
from .project_layout import paths as layout_paths


SLIDE_DECK_INSTRUCTIONS = f"""
For a new HTML slide deck or presentation, read and use the html-slide-deck skill:
{SLIDE_DECK_DIR / 'SKILL.md'}
Its reusable HTML template is at {SLIDE_DECK_DIR / 'assets' / 'deck.html'}.
Use it for the deck's design; use the linked notebook for computed figures and tables.
New project reports already start from this template. Keep an existing report's
design when extending it, and honor an explicitly requested alternative design.
"""

INSTRUCTIONS = '''The following project rules, including the slide-deck skill, apply
only to the project identified by the terminal's current context.
''' + SLIDE_DECK_INSTRUCTIONS + r'''
This terminal is working in an analysis project. These rules apply only while
the current `gusnb project context` identifies that project. Outside a project,
use the normal notebook/HTML workflow; earlier project rules no longer apply.
Run `gusnb project context` at the start of each request for the project paths,
approved sources and uv interpreter. Use the existing HTML editor and terminal chat.
Use the current context's notebook, report and layout_paths; they take precedence
over paths from earlier requests when this terminal changes project directories
or the user selects a project with create/open. Resolve layout_paths relative to
context.path. Changing browser tabs does not change the terminal's project.

Default workflow for a plot, table, number or other computed content in the HTML:
1. Read `gusnb here --full` and `gusnb project report` to preserve the requested
   layout and surrounding content. Remember the report revision before editing.
2. Use only the user's authorized inputs. Register each exact approved file path
   with `gusnb project source PATH --description "contents and origin"`, then read that original path directly from
   context.sources[].path. Registration records a reference, not a dataset copy.
   Reuse already registered paths. Do not copy, rename, hash the entire input,
   or create an ID-named dataset folder to make data part of a project. Files
   can remain outside the project. For large data, select needed columns and
   partitions or process chunks; do not load the whole dataset just to inspect it.
   If context marks a source as legacy_snapshot, its original path was not saved;
   ask for the original path when needed and relink with `gusnb project source LABEL PATH`.
   Never guess the original location. Save processed datasets/cohorts under
   layout_paths.processed (data/processed), figures under layout_paths.outputs,
   and reports under layout_paths.reports. If the user explicitly asks
   for dummy/synthetic data, generate it in the notebook with a fixed random seed,
   label it synthetic, and record that choice. Synthetic examples need no external
   source registration. Never substitute dummy data for a real-data request.
3. Prepare the uv environment using `gusnb project prepare` if needed, then poll
   `gusnb project status` until complete. Target the linked notebook explicitly
   with `gusnb -n NOTEBOOK_PATH ...`, using context.notebook. Check its kernel
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
   After producing or updating a dataset, record it with `gusnb project data --file -`
   using JSON containing path, description (what the dataset represents), transformation
   (filters, exclusions, joins, cleaning and other changes), parameters, inputs
   (approved raw-source labels/paths or already recorded processed paths), and
   producer: {"path": "code/notebooks/analysis.ipynb", "cell_id": "producing-cell"}.
   A Python script can be the producer instead; omit cell_id in that case. Paths are
   relative to context.path. Use the actual paths from layout_paths for older projects.
   Set synthetic:true and inputs:[] only for explicitly requested synthetic data.
   Record only files that exist and code actually used; never invent an execution,
   input relationship, cohort count or human approval. Re-record after regeneration.
   `gusnb project data` shows the catalog. GusNotebook maintains data/README.md and
   data/lineage.json with input revisions, producing code, actor/request context
   and history links. Keep additional scientific notes outside the README's managed
   catalog markers. Do not edit lineage.json as a substitute for recording changes.
5. After successful execution, export the producing cell:
   `gusnb -n NOTEBOOK export-output CELL_ID --file FIGURE_HTML`, saving under layout_paths.outputs.
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
   this task's source, notebooks and uv definitions using the Git paths below,
   then commit with a meaningful message. Follow the user's commit/push preferences.
   Reuse the existing GitHub remote and credentials. If no remote is configured,
   keep commits local and ask for the intended GitHub repository before connecting
   or pushing. Do not create a remote repository or invent a Git identity.
   Never stage tests, data, environments, reports, outputs, secrets or unrelated files.

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


def instructions_for(project):
    """Use the actual layout, including notebooks outside code/ in older projects."""
    paths = layout_paths(project)
    definitions = paths['dependencies'].rstrip('/')
    prefix = '' if definitions == '.' else definitions + '/'
    tracked = ['code/', paths['notebooks'] + '/',
               *(prefix + name for name in ('pyproject.toml', 'uv.lock', '.python-version'))]
    return (INSTRUCTIONS + '\nGit paths relative to the linked project root: ' +
            ', '.join('`' + path + '`' for path in tracked) + '.\n' +
            'Exclude tests in `' + paths['tests'] + '/` and reports in `' + paths['reports'] +
            '/`. Select the changed files; never stage the whole project root.\n')
