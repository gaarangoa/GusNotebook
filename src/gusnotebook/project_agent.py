"""Instructions for the existing Claude/Codex terminal, scoped to a project."""

INSTRUCTIONS = '''
This terminal is working on a report-first GusNotebook analysis project. NB_PROJECT identifies it.
Project instructions take precedence over generic instructions to add ordinary notebook cells.
Run `gusnb project context` at the start of each request for the current project, analyses,
approved source metadata and uv interpreter. Use `gusnb project --help` for commands.
Start with the user's HTML report/slide narrative. Ground computed claims in reusable Python
modules defining analyze(data, parameters), where data maps approved source IDs to DataFrames.
Use only the approved inputs listed in the context. Never read unrelated data, credentials or
other directories, add approvals, or change acceptance state. uv isolates dependencies only;
these are workflow instructions, not a filesystem sandbox. Ask the user to approve missing data.
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
candidates in the UI. Never accept results on their behalf. `run ID --replay RUN_ID` replays a
saved run; `run ID --package` builds a wheel/API and verifies it on original and synthetic data.
Read report HTML and revision with `gusnb project report`. Write it using
`gusnb project report --file updated.html --revision SAVED_REVISION`. Preserve gusnb markers and
accepted result sections. Keep calculations in Python and do not invent numbers in the narrative.
Before replying, persist your explanation, choices and limitations with `gusnb project note -`
via stdin. Prompts and broker operations are logged automatically; this command records your
assistant response in the durable audit log. Do not store credentials in messages or code.
'''
