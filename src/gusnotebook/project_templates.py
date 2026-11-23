"""Project scaffold and backwards-compatible structured analysis templates."""

import html
import json
import os
from pathlib import Path

SLIDE_DECK_DIR = Path(__file__).parent / "bundled_skills" / "html-slide-deck"

DEPENDENCIES = ["numpy>=1.26,<3", "pandas>=2.2,<4", "scipy>=1.12,<2",
                "scikit-learn>=1.5,<2", "pyarrow>=17", "ipykernel>=6",
                "setuptools>=75", "wheel>=0.44"]


def pyproject(name, extra=(), source_directory="code/src"):
    return ('[project]\nname = ' + json.dumps(name.replace('_', '-')) + '\nversion = "0.1.0"\n'
            'requires-python = ">=3.12,<3.13"\ndependencies = ' + json.dumps(DEPENDENCIES + list(extra)) +
            '\n\n[build-system]\nrequires = ["setuptools>=75", "wheel>=0.44"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            '[tool.setuptools.packages.find]\nwhere = [' + json.dumps(source_directory) + ']\n')


PCA_CODE = '''"""Two-component PCA; explicit numeric features, complete cases, standardized by default."""
import numpy as np
from sklearn.decomposition import PCA
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


def analyze(data, parameters):
    """Accept a mapping of source IDs to DataFrames; return JSON-compatible results.

    parameters: source, columns (at least two), standardize (default True).
    Missing rows are excluded and reported. No path or dataset is hardcoded.
    """
    source = parameters.get("source")
    if source not in data:
        raise ValueError("Select an approved source")
    columns = parameters.get("columns", [])
    if len(columns) < 2 or len(set(columns)) != len(columns):
        raise ValueError("Select at least two distinct numeric columns")
    frame = data[source].loc[:, columns]
    if not all(np.issubdtype(dtype, np.number) for dtype in frame.dtypes):
        raise ValueError("PCA features must be numeric")
    clean = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 3 or len(clean) > 50000:
        raise ValueError("PCA requires 3 to 50,000 complete rows in this first release")
    if (clean.var() == 0).any():
        raise ValueError("Remove constant features before PCA")
    pipeline = Pipeline([
        ("scale", StandardScaler() if parameters.get("standardize", True) else "passthrough"),
        ("pca", PCA(n_components=2, svd_solver="full", random_state=0)),
    ])
    coordinates = pipeline.fit_transform(clean)
    variance = pipeline.named_steps["pca"].explained_variance_ratio_
    return {
        "title": "Principal component analysis",
        "summary": "PCA of " + ", ".join(columns) + ". Complete cases; " +
                   ("standardized features." if parameters.get("standardize", True) else "unscaled features."),
        "metrics": {"rows_analyzed": len(clean), "rows_excluded": len(frame) - len(clean),
                    "PC1_variance": float(variance[0]), "PC2_variance": float(variance[1])},
        "chart": {"x_label": "PC1", "y_label": "PC2", "points": [
            {"x": float(x), "y": float(y), "label": str(index)}
            for index, (x, y) in zip(clean.index, coordinates)]},
        "table": {"columns": ["feature", "PC1_loading", "PC2_loading"], "rows": [
            [column, float(a), float(b)] for column, a, b in
            zip(columns, *pipeline.named_steps["pca"].components_)]},
    }
'''

def d3_source():
    source = (Path(__file__).parent / "static/vendor/d3-source.js").read_text()
    return json.loads(source.split(" = ", 1)[1].rstrip(";\n"))


def renderer():
    return (Path(__file__).parent / "static/project-renderer.js").read_text()


def result_html(result, analysis_id, run_id):
    payload = json.dumps(result, allow_nan=False).replace("<", "\\u003c")
    return (f'<section class="analysis-result" data-analysis="{analysis_id}" data-run="{run_id}">'
            f'<button class="analysis-link" data-analysis="{analysis_id}">Analysis · {analysis_id} · {run_id}</button>'
            '<div class="analysis-content"></div><script type="application/json" class="analysis-data">' +
            payload + '</script></section>')


def deck(title):
    template = (SLIDE_DECK_DIR / "assets" / "deck.html").read_text(encoding="utf-8")
    template = template.replace("Analysis title", html.escape(title))
    # Retain rendering for the legacy structured-analysis endpoints. Ordinary
    # notebook exports carry their own renderer inside the exported fragment.
    return template.replace('</body>', '<script>' + d3_source() + '</script><script>' + renderer() + '</script></body>')


STYLE = '''body{margin:0;background:#f4f5f8;color:#172033;font:16px/1.6 system-ui}
main{max-width:1040px;margin:32px auto;padding:40px;background:white;border-radius:12px}
h1{font-size:36px}h2{font-size:24px}.analysis-result{margin:24px 0;padding:20px;border:1px solid #ccd3dd;border-radius:8px}
.analysis-link{font:12px system-ui;color:#486481;background:none;border:0;cursor:pointer;padding:0}
.analysis-metrics{display:flex;gap:24px;flex-wrap:wrap}.analysis-metrics strong{display:block;font-size:22px}
svg{width:100%;max-height:540px}table{border-collapse:collapse}td,th{padding:6px 12px;text-align:left;border-bottom:1px solid #dde2eb}
@media(max-width:700px){main{margin:0;padding:16px}}'''


def empty_notebook(name, project_id, report="../reports/deck.html"):
    import nbformat
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell(
        "# " + name + "\nLinked report: " + report + ". Ask the agent to add Python and D3 code here.")])
    nb.metadata["gusproject"] = {"project_id": project_id, "report": report}
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    return nbformat.writes(nb)


def notebook(analysis_id, parameters, sources, code, result=None, run_id=None, rendered=None, python=None, layout=None):
    import nbformat
    nb = nbformat.v4.new_notebook()
    layout = layout or {"reports": "reports", "manifest": "gusproject.json", "notebooks": "notebooks"}
    loads = []
    for source in sources:
        path = f"data/inputs/{source['id']}/{source['sha256']}{source['suffix']}"
        method = "read_csv" if source["suffix"] == ".csv" else "read_parquet"
        loads.append(f"    {source['id']!r}: pd.{method}(root / {path!r}),")
    nb.cells = [nbformat.v4.new_markdown_cell(
        f"# {analysis_id}\nRun: `{run_id or 'not yet executed'}`. The linked report uses this analysis.\n"
        "Uses the project's uv environment. Notebook execution is separate from recorded project runs."),
        nbformat.v4.new_code_cell(code + "\n\n# Run with the approved inputs.\nfrom pathlib import Path\nimport sys\nimport pandas as pd\n"
            "root = next((p for p in (Path.cwd(), *Path.cwd().parents) if (p / " + repr(layout["manifest"]) + ").is_file()), None)\n"
            "if root is None: raise RuntimeError('Open this notebook inside its analysis project')\n"
            "sys.path.insert(0, str(root / 'code/src'))\n"
            "data = {\n" + "\n".join(loads) + "\n}\nparameters = " + repr(parameters) + "\nresult = analyze(data, parameters)\nresult"),
        nbformat.v4.new_code_cell("import json\nfrom IPython.display import HTML, display\n"
            "view = (root / " + repr(layout["reports"] + '/views/result.html') + ").read_text()\n"
            "display(HTML(view.replace('GUS_RESULT_JSON', json.dumps(result, allow_nan=False).replace('<', '\\\\u003c'))))")]
    for index, cell in enumerate(nb.cells):
        cell.id = f"{analysis_id}-{index}"
    if result is not None:
        nb.cells[1].execution_count = 1
        nb.cells[1].outputs = [nbformat.v4.new_output("display_data", data={"application/json": result})]
        nb.cells[2].execution_count = 2
        nb.cells[2].outputs = [nbformat.v4.new_output("display_data", data={"text/html": rendered})]
    nb.metadata["gusproject"] = {"analysis_id": analysis_id, "run_id": run_id, "code": code}
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    if python:
        nb.metadata["kernelspec"]["notebook_python"] = python
    nb.metadata["gusproject"]["report"] = os.path.relpath(layout["reports"] + "/deck.html", layout["notebooks"])
    return nbformat.writes(nb)
