"""GusNotebook — notebooks with embedded Claude Code terminals.

Importing this package is deliberately cheap: it starts no server, opens no
notebook, and touches no disk. `app.py` does all of that, and it's imported by
the `gusnotebook` entry point rather than from here, so `import gusnotebook` in a
cell (or a test) doesn't launch a second copy of the app.
"""

__version__ = "0.1.0"
