"""Project paths; existing workspaces keep their paths and running kernels."""

LAYOUT = "code-data-results"


def paths(project):
    if project.get("layout") == LAYOUT:
        return {"dependencies": "code", "notebooks": "code/notebooks",
                "processed": "data/processed", "data_readme": "data/README.md", "lineage": "data/lineage.json",
                "reports": "results/reports", "outputs": "results/outputs",
                "environment": "data/environment", "manifest": "data/.gusnotebook/project.json",
                "audit": "data/.gusnotebook/audit", "tests": "code/tests"}
    return {"dependencies": ".", "notebooks": "notebooks", "reports": "reports",
            "processed": "data/processed", "data_readme": "data/README.md", "lineage": "data/lineage.json",
            "outputs": "data/derived", "environment": "environment", "manifest": "gusproject.json",
            "audit": "audit", "tests": "tests"}


GITIGNORE = """# Only source code and notebooks are versioned.
/*
!/.gitignore
!/code/
/code/tests/
**/__pycache__/
**/.ipynb_checkpoints/
*.egg-info/
.env
.env.*
"""
