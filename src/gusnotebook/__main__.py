"""`python -m gusnotebook` — the same thing the `gusnotebook` command runs.

Worth having: a venv that isn't on PATH still has its interpreter, so this is the
one invocation that always works.
"""

from .app import main

if __name__ == "__main__":
    main()
