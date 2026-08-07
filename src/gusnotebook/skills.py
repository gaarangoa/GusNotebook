"""Skills — small reusable snippets and practices, for Claude and the notebook.

A skill is one directory holding a `SKILL.md`: YAML front matter (`name`,
`description`) and a body that says when to use it, when not to, and shows the
code. Deliberately the format Claude Code already reads, so the same file serves
two consumers without being written twice:

  * **Claude** — `plugin_args()` points a session at `SKILLS_DIR` with
    `--plugin-dir`, so `/csv-peek` works and Claude can cite the practice.
  * **The notebook** — `code_of()` pulls the first Python block out of the body
    for the Skills picker, which inserts it as a cell. No model call: the point
    of a snippet is that you already know what you want.

The scope is a deliberate limit, and it's the whole reason this is useful. A
skill is a snippet plus the judgement around it — a dozen lines you'd otherwise
retype, with a note on when it's the wrong tool. It is *not* a framework, a
class hierarchy, or anything with its own dependencies: a library that needs
installing belongs in a package where it can be versioned and tested, not
copy-pasted out of a markdown file into a cell. Enforcing that is the job of
whoever writes the skill, but `MAX_BYTES` catches the extreme case.

Files are plain markdown on disk, so a skill can be written in the app, edited
in any editor, read without this code, and committed to git.
"""

import pathlib
import re
import shutil
import threading

from . import paths

# Laid out as a Claude Code plugin, because that's what makes `--plugin-dir`
# work: a manifest beside a `skills/` directory of SKILL.md files.
#
# In the state directory, not the package: skills are the user's own writing,
# created and edited through the app, and an upgrade that replaced site-packages
# would delete every one of them.
PLUGIN_DIR = paths.state("skills")
SKILLS_DIR = PLUGIN_DIR / "skills"
MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"

PLUGIN_NAME = "notebook-skills"

# A snippet, not a library. Well past anything that belongs in one cell, so it
# only trips on the case this format can't serve — see the module docstring.
MAX_BYTES = 64 * 1024

FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
# ```python / ```py, or a bare fence: a snippet is Python here either way.
BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)

_lock = threading.RLock()


def _slug(name):
    """A directory name from a skill name: also the `/name` Claude answers to."""
    keep = [c.lower() if c.isalnum() else "-" for c in (name or "").strip()]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "skill"


def ensure_plugin():
    """Create the plugin skeleton if it isn't there.

    Idempotent, and called before anything reads or writes: the directory is
    gitignored per-machine, so a fresh clone has no `skills/` at all and every
    entry point has to cope with that rather than assume a previous run made it.
    """
    with _lock:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        if not MANIFEST.exists():
            MANIFEST.write_text(
                '{\n'
                f'  "name": "{PLUGIN_NAME}",\n'
                '  "description": "Scripting snippets and practices from the '
                'notebook app.",\n'
                '  "version": "0.1.0"\n'
                '}\n', encoding="utf-8")
    return PLUGIN_DIR


def plugin_args():
    """`--plugin-dir` arguments for a Claude session, or [] if there's nothing.

    Empty when no skill exists: passing a plugin with an empty `skills/` adds a
    name to Claude's `/`-menu that does nothing, which is worse than absent.
    """
    ensure_plugin()
    return ["--plugin-dir", str(PLUGIN_DIR)] if _dirs() else []


def _dirs():
    if not SKILLS_DIR.is_dir():
        return []
    return sorted((d for d in SKILLS_DIR.iterdir()
                   if d.is_dir() and (d / "SKILL.md").is_file()),
                  key=lambda d: d.name)


# --- parsing ---

def parse(text):
    """Split a SKILL.md into front matter and body.

    Hand-rolled rather than via a YAML dependency: the front matter this format
    needs is `name` and `description`, both plain one-line strings, and the file
    is more likely to be edited by hand than generated.
    """
    m = FRONT.match(text or "")
    if not m:
        return {}, (text or "").strip()
    meta = {}
    for line in m.group(1).split("\n"):
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, m.group(2).strip()


def code_of(body):
    """The first fenced block in the body — what the picker puts in a cell.

    The first, not all of them: later blocks in a well-written skill are usually
    a counter-example or the wrong way to do it, and concatenating them would
    produce a cell that contradicts itself.
    """
    m = BLOCK.search(body or "")
    return m.group(1).rstrip("\n") if m else ""


def _read(d):
    text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    meta, body = parse(text)
    return {
        "id": d.name,
        "name": meta.get("name") or d.name,
        "description": meta.get("description") or "",
        "body": body,
        "code": code_of(body),
        "path": str(d / "SKILL.md"),
    }


# --- CRUD ---

def all_skills():
    ensure_plugin()
    out = []
    for d in _dirs():
        try:
            out.append(_read(d))
        except OSError:
            continue          # unreadable file: skip it, don't fail the list
    return out


def get(sid):
    d = SKILLS_DIR / _slug(sid)
    if not (d / "SKILL.md").is_file():
        raise ValueError(f"no such skill: {sid}")
    return _read(d)


def _compose(name, description, body):
    desc = " ".join((description or "").split())       # front matter is one line
    return (f"---\nname: {_slug(name)}\ndescription: {desc}\n---\n\n"
            f"{(body or '').strip()}\n")


def save(name, description, body, sid=None):
    """Create a skill, or update the one at `sid`.

    Renaming moves the directory, because the directory name *is* the `/command`
    Claude offers — leaving it behind would give a renamed skill its old name in
    Claude's menu and its new one in the picker.
    """
    ensure_plugin()
    name = (name or "").strip()
    if not name:
        raise ValueError("a skill needs a name")
    body = body or ""
    if len(body.encode()) > MAX_BYTES:
        raise ValueError(
            "too long for a skill — this format is for snippets and practices; "
            "something this size belongs in an importable module")

    slug = _slug(name)
    with _lock:
        d = SKILLS_DIR / slug
        old = SKILLS_DIR / _slug(sid) if sid else None
        if old and old != d and old.is_dir():
            if d.exists():
                raise ValueError(f"a skill called {slug} already exists")
            old.rename(d)
        elif not sid and d.exists():
            raise ValueError(f"a skill called {slug} already exists")
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            _compose(name, description, body), encoding="utf-8")
    return _read(d)


def delete(sid):
    d = SKILLS_DIR / _slug(sid)
    if not d.is_dir():
        return False
    with _lock:
        shutil.rmtree(d, ignore_errors=True)
    return True


# --- the starter set ---

# Enough to show what a skill is for without pretending to be a library. Each
# one is a snippet plus the judgement that goes with it — including when not to
# use it, which is the part a bare snippet can't carry.
#
# `work-on-this-cell` is the odd one out and ships anyway: it documents this
# app's own feature, so a fresh install where Claude doesn't know `gusnb` exists
# is exactly the install that needs it. It's also the one starter with no Python
# block — a practice rather than a snippet — so the picker reports "no code
# block" rather than inserting an empty cell.
STARTERS = [
    ("work-on-this-cell",
     "Iterate on the notebook cell the user is parked on: write it, run it, fix "
     "what fails.",
     """Use when the user asks for something that plainly refers to the cell
they're looking at — "extract the site and n columns", "make this handle missing
dates", "why is this empty?". Their active cell is already in your context: the
app injects it on every prompt, so you don't need to ask which cell or fetch it.

The loop, from anywhere:

```bash
gusnb here                    # the focused cell: source, output, and its id
gusnb here - --run            # replace it from stdin and run it
gusnb undo <cell_id>          # put back what a replace overwrote
```

`here - --run` prints the cell's output back to you, including the traceback if
it raised. That's the signal to iterate on: rewrite, run again, repeat. Three
attempts is usually the point at which the problem is your assumption about the
data rather than the code — stop and say what you found instead of trying a
fourth variation.

Pin the id `here` prints and use it for the rest of the loop. Don't re-run
`here` between attempts: the user may have clicked another cell while you
worked, and you'd silently start editing that one instead.

Two things worth being careful about, because neither is recoverable by the
model:

- **Replacing is destructive.** The old source goes on that cell's undo stack
  (the ↶ in the browser, one step per replace, independent per cell), but the
  user still has to notice and click it. Don't rewrite a cell that already works
  unless they asked you to. When the request is additive — "now also plot it" —
  `gusnb add -` a new cell below instead.
- **"It ran" is not "it's right."** No exception only means no exception. Read
  the output and say whether it's what they asked for; if the frame came back
  empty or a column is all NaN, that's the finding, not a success.

Not for work that isn't about one cell. A refactor across the notebook, or
anything where the answer is a file rather than a cell, is ordinary editing —
use your normal tools and leave the notebook's cells alone."""),

    ("csv-peek",
     "Load a messy CSV without dtype surprises: read as text, convert on purpose.",
     """Use when a CSV is new to you, or has mixed types, blanks, or ids with
leading zeros. Pandas guesses a dtype per column and quietly turns `007` into
`7`, `1/2` into a date, and a column with one stray `"n/a"` into `object`.
Reading everything as text first makes every conversion yours.

Not for files big enough to matter — strings cost more memory than the parsed
types. Past a few hundred MB, pass an explicit `dtype=` map instead.

```python
import pandas as pd

df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
print(df.shape)

# Convert deliberately, one column at a time. errors="coerce" turns anything
# unparseable into NaN rather than raising, so the count below tells you how
# much of the column was junk.
num = pd.to_numeric(df["amount"], errors="coerce")
print(f"{num.isna().sum()} of {len(num)} values in 'amount' did not parse")

df.head()
```"""),

    ("df-overview",
     "First look at a DataFrame: shape, dtypes, missingness, and cardinality.",
     """Run this before analysing an unfamiliar table. It answers the four
questions that decide what you can do with it — how big, what types, what's
missing, and which columns are categorical — in one output instead of four
half-remembered calls.

```python
import pandas as pd


def overview(df):
    "One row per column: dtype, nulls, distinct values, and an example."
    return pd.DataFrame({
        "dtype": df.dtypes.astype(str),
        "nulls": df.isna().sum(),
        "null_%": (df.isna().mean() * 100).round(1),
        "distinct": df.nunique(dropna=True),
        # A real value, not the first row, which is often blank in dirty data.
        "example": [df[c].dropna().iloc[0] if df[c].notna().any() else None
                    for c in df.columns],
    })


print(f"{len(df):,} rows x {len(df.columns)} columns")
overview(df)
```"""),

    ("plot-defaults",
     "A readable matplotlib figure: labelled axes, no chartjunk, room for the labels.",
     """Use for any plot someone other than you will look at. Matplotlib's
defaults are small, unlabelled, and boxed in; four lines fix it. An axis without
units is the single most common reason a chart has to be explained out loud.

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
ax.plot(x, y, lw=1.6)

ax.set_xlabel("Date")
ax.set_ylabel("Revenue (GBP, thousands)")   # always name the unit
ax.set_title("Revenue by month")
ax.grid(alpha=.3, lw=.6)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)      # the box adds nothing
fig.tight_layout()                          # stop long labels being clipped
```"""),

    ("sql-to-df",
     "Query a database into a DataFrame with parameters, never string formatting.",
     """Use whenever a query takes a value from a variable. Interpolating with
f-strings or `%` is how SQL injection happens, and it also breaks on any value
containing a quote — a surname like O'Brien is enough. Placeholders let the
driver handle quoting and typing.

```python
import pandas as pd
from sqlalchemy import create_engine, text

engine = create_engine(url)          # e.g. "postgresql+psycopg://user@host/db"

# :placeholders, with values passed separately. Never build this with f-strings.
query = text("select id, site, measured_at, value "
             "from readings "
             "where site = :site and measured_at >= :since "
             "order by measured_at")

with engine.connect() as conn:
    df = pd.read_sql(query, conn, params={"site": site, "since": since})

print(f"{len(df):,} rows")
df.head()
```"""),

    ("checkpoint",
     "Cache an expensive step to disk so a kernel restart doesn't redo it.",
     """Use for the slow cell in a notebook — a long query, a download, an
hour of feature building. Reruns read the file instead, so restarting the kernel
costs seconds rather than the whole pipeline.

Two rules. Put the checkpoint path somewhere ignored by git, and delete it when
the *inputs* change: a cache keyed only on a filename will happily serve you
yesterday's data all day. If that's a real risk, put a date or a parameter in
the name.

```python
import pathlib
import pandas as pd

CACHE = pathlib.Path("cache")
CACHE.mkdir(exist_ok=True)


def checkpoint(name, build):
    "Return the cached DataFrame, or build it and cache it."
    path = CACHE / f"{name}.parquet"     # parquet keeps dtypes; csv doesn't
    if path.exists():
        print(f"cached: {path}")
        return pd.read_parquet(path)
    df = build()
    df.to_parquet(path, index=False)
    print(f"built and cached: {path}")
    return df


df = checkpoint("readings-2026-q1", lambda: pd.read_sql(query, engine))
```"""),
]


def install_starters():
    """Write the starter skills, once, if the user has none.

    Only when the directory is empty: this is a first-run demonstration of the
    format, and re-adding a starter the user deliberately deleted would make
    deletion feel broken.
    """
    ensure_plugin()
    if _dirs():
        return []
    made = []
    for name, description, body in STARTERS:
        try:
            made.append(save(name, description, body))
        except (ValueError, OSError):
            continue
    return made
