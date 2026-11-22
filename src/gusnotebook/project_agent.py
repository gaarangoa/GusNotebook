"""Instructions for the existing Claude/Codex terminal, scoped to a project."""

WORKSPACE_INSTRUCTIONS = """
For a report or slide deck grounded in data, use the NORMAL GusNotebook workspace:
the existing terminal chat, HTML editor and Jupyter notebook tabs. You can create
the linked files with `gusnb project create NAME /absolute/parent/folder`; it opens
the report and notebook automatically. Or use `gusnb project list` then
`gusnb project open PROJECT_ID`. Read `gusnb project context` for the workflow and
paths. Prepare uv, register only data paths the user authorized, write reusable
Python, run it, publish its actual results, and record your explanation. Do not
ask the user to fill analysis/code/parameter forms or copy outputs by hand.
"""

INSTRUCTIONS = '''
This terminal is linked to a report and Jupyter notebook in the NORMAL GusNotebook workspace.
Project instructions take precedence over generic instructions to add ordinary notebook cells.
Run `gusnb project context` at the start of each request for the current project, analyses,
approved source metadata and uv interpreter. Use `gusnb project --help` for commands.
Start with the user's HTML report/slide narrative. Ground computed claims in reusable Python
modules defining analyze(data, parameters), where data maps approved source IDs to DataFrames.
Use only the approved inputs listed in the context. Never read unrelated data, credentials or
other directories or change acceptance state. uv isolates dependencies only;
these are workflow instructions, not a filesystem sandbox. Register only exact paths explicitly supplied or approved by the user with `gusnb project source ID PATH`. Ask about missing data.
Keep parameters separate from code. Validate input types/columns and report exclusions. Prefer
sklearn Pipeline/ColumnTransformer/estimators where appropriate. Never fabricate results.
Use `gusnb project prepare` if the environment is not ready. The project uses uv with
UV_PROJECT_ENVIRONMENT=environment/.venv and a saved uv.lock. Do not use Docker.
To save analysis ID, send JSON to `gusnb project save ID -` through stdin:
{"code":"complete module source", "parameters":{}, "source_ids":["approved_id"], "revision":"new"}.
For existing code use its code_revision from context as revision. Use this API instead of direct
file writes so the before/after source and generated notebook are recorded in project history.
Return JSON-compatible finite values from analyze: {"title":"...", "summary":"...",
"metrics":{"value":0.25}, "chart":{"x_label":"...","y_label":"...",
"points":[{"x":1.0,"y":2.0,"label":"..."}]}, "table":{"columns":["..."],"rows":[[1]]}}.
Chart/table are optional; at most 50,000 points or rows. Python calculates; the shared D3 renderer
presents results in the report and notebook. No hardcoded dataset paths or row counts.
Run `gusnb project run ID`, then poll `gusnb project status` until the job finishes.
Use `gusnb project result RUN_ID` to inspect the real output. The user previews and accepts
candidates in the normal HTML editor. Use `gusnb project publish RUN_ID` to put
completed results in the report immediately, with their notebook links. Publication
is a draft, not human acceptance. Never accept results on the user's behalf. `run ID --replay RUN_ID` replays a
saved run; `run ID --package` builds a wheel/API and verifies it on original and synthetic data.
Read report HTML and revision with `gusnb project report`. Write it using
`gusnb project report --file updated.html --revision SAVED_REVISION`. Preserve gusnb markers and
accepted result sections. The project creates notebooks/analysis.ipynb; prefer analysis as the first section ID. Each saved analysis has a runnable .ipynb with its actual Python code and results, using the project uv interpreter. Keep calculations in Python and do not invent numbers in the narrative.
Before replying, persist your explanation, choices and limitations with `gusnb project note -`
via stdin. Prompts and broker operations are logged automatically; this command records your
assistant response in the durable audit log. Do not store credentials in messages or code.
'''
