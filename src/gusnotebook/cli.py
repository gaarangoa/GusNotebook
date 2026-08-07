"""`gusnb` — write code into the running notebook: same kernel, same document.

This is how an agent puts code in the notebook: `add` appends a cell, runs it on
the live kernel, and prints the output. The cell and its result appear in the
browser immediately.

    gusnb list
    gusnb add "df.head()"                  # add code cell + run it
    gusnb add --md "## Findings"           # add markdown cell
    gusnb add --no-run "import pandas"     # add without running
    gusnb run <cell_id>                    # re-run a cell
    gusnb run-all
    gusnb set <cell_id> "new source"       # replace a cell's source
    gusnb here                             # the cell the user is parked on
    gusnb here - --run                     # replace that cell and run it
    gusnb undo <cell_id>                   # put back what a replace overwrote
    gusnb delete <cell_id>
    gusnb clear
    gusnb restart

The browser can have several notebooks open in tabs, each with its own kernel.
Commands act on the first open notebook unless you name one with -n:

    gusnb tabs                             # what's open, and on which python
    gusnb -n analysis.ipynb list           # target one tab
    gusnb -n analysis.ipynb add "df.head()"
    gusnb open analysis.ipynb              # open a tab (creates the file)
    gusnb env                              # this notebook's interpreter
    gusnb env .venv312                     # switch it, restarting the kernel

Reads code from stdin when the source argument is "-", which avoids quoting
pain for multi-line code:

    gusnb add - <<'PY'
    import pandas as pd
    df = pd.DataFrame({"a": [1, 2]})
    df
    PY

Talks to the app over HTTP, so it works from any directory — including one that
isn't the notebook's. `$NB_URL` (or `$PORT`) points it at a non-default port;
GusNotebook sets `NB_URL` in every terminal it opens.
"""

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

PORT = os.environ.get("PORT", "8888")
BASE = os.environ.get("NB_URL", f"http://127.0.0.1:{PORT}")

# Which notebook tab commands act on. Set from -n / $NB_NOTEBOOK; empty means
# "whatever the app has open", which is what a single-notebook session wants.
TARGET = os.environ.get("NB_NOTEBOOK", "")


def target_path(value):
    """Absolute path for a -n argument, resolved against the current directory."""
    return str(pathlib.Path(value).expanduser().resolve())


def call(path, method="GET", body=None):
    if TARGET:
        sep = "&" if "?" in path else "?"
        path += sep + urllib.parse.urlencode({"notebook": TARGET})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # The app answered — surface its message, not "is it running?".
        # (HTTPError is a URLError subclass, so it has to be caught first.)
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        sys.exit(body.get("error") or f"HTTP {e.code} from {path}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach the app at {BASE} — is it running? ({e})")


# --- output formatting ---

RED, DIM, BOLD, RESET = "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def strip_ansi(s):
    import re
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)


def show_outputs(outputs, indent="  "):
    if not outputs:
        print(f"{indent}{DIM}(no output){RESET}")
        return
    for o in outputs:
        t = o.get("output_type")
        if t == "stream":
            color = RED if o.get("name") == "stderr" else ""
            for line in o.get("text", "").rstrip("\n").split("\n"):
                print(f"{indent}{color}{line}{RESET}" if color else f"{indent}{line}")
        elif t == "error":
            tb = "\n".join(o.get("traceback") or [])
            text = strip_ansi(tb) if tb else f"{o.get('ename')}: {o.get('evalue')}"
            for line in text.rstrip("\n").split("\n"):
                print(f"{indent}{RED}{line}{RESET}")
        elif t in ("execute_result", "display_data"):
            d = o.get("data", {})
            if "text/plain" in d:
                for line in d["text/plain"].rstrip("\n").split("\n"):
                    print(f"{indent}{line}")
            for mime in d:
                if mime.startswith("image/"):
                    print(f"{indent}{DIM}[{mime} — rendered in the notebook pane]{RESET}")


def show_cell(c, index=None):
    kind = c.get("cell_type", "code")
    count = c.get("execution_count")
    label = f"[{count if count is not None else ' '}]" if kind == "code" else f"[{kind}]"
    prefix = f"{index}. " if index is not None else ""
    print(f"{BOLD}{prefix}{label}{RESET} {DIM}{c['id']}{RESET}")
    # Why this cell looks the way it does, when something wrote it rather than the
    # user. Worth showing before the source: on a re-read mid-loop it says whether
    # this is already your own handiwork.
    if c.get("claude_prompt"):
        print(f"    {DIM}✳ asked: {c['claude_prompt']}{RESET}")
    if c.get("prompt"):
        print(f"    {DIM}AI prompt: {c['prompt']}{RESET}")
    src = (c.get("source") or "").rstrip("\n")
    for line in (src.split("\n") if src else [f"{DIM}(empty){RESET}"]):
        print(f"    {line}")
    if kind == "code" and c.get("outputs"):
        show_outputs(c["outputs"], indent="    ")
    print()


def read_source(value):
    if value == "-":
        return sys.stdin.read()
    return value


# --- commands ---

def cmd_list(args):
    data = call("/api/notebook")
    print(f"{DIM}{data['path']} · kernel {data.get('kernel_status')}"
          f" · {data.get('kernel_python')}{RESET}\n")
    for i, c in enumerate(data["cells"]):
        show_cell(c, i)


def cmd_add(args):
    cell_type = "markdown" if args.md else ("raw" if args.raw else "code")
    source = read_source(args.source) if args.source else ""
    body = {"cell_type": cell_type, "source": source}
    if args.after:
        body["after"] = args.after
    if args.index is not None:
        body["index"] = args.index
    cell = call("/api/cells", "POST", body)
    print(f"added {cell['cell_type']} cell {cell['id']}")

    if cell_type == "code" and source.strip() and not args.no_run:
        result = call(f"/api/cells/{cell['id']}/run", "POST", {})
        show_outputs(result.get("outputs"))


def cmd_run(args):
    body = {}
    if args.source:
        body["source"] = read_source(args.source)
    result = call(f"/api/cells/{args.cell_id}/run", "POST", body)
    if "error" in result:
        sys.exit(result["error"])
    show_outputs(result.get("outputs"))


def cmd_run_all(args):
    result = call("/api/run-all", "POST", {})
    print(f"ran {result.get('ran', 0)} cell(s)\n")
    for r in result.get("results", []):
        print(f"{BOLD}{r['cell_id']}{RESET}")
        show_outputs(r.get("outputs"), indent="    ")


def cmd_set(args):
    # undoable: this replaces source the user may have written by hand, so the
    # old version goes on the cell's undo stack and the ↶ appears in the browser.
    cell = call(f"/api/cells/{args.cell_id}", "PATCH",
                {"source": read_source(args.source), "undoable": True})
    if "error" in cell:
        sys.exit(cell["error"])
    print(f"updated {args.cell_id}")
    if args.run and cell.get("cell_type") == "code":
        show_outputs(call(f"/api/cells/{args.cell_id}/run", "POST", {}).get("outputs"))


def cmd_here(args):
    """The cell the user is parked on — read it, or replace and run it.

    This is the "work on what I'm looking at" entry point: no id to be told, and
    the id it prints is worth pinning for the rest of a fix-and-rerun loop, since
    the user may click elsewhere while you work.
    """
    info = call("/api/here")
    cell = info.get("cell")
    if not cell:
        sys.exit(info.get("note") or "no cell is focused")
    cid = info["cell_id"]

    # Target the notebook the caret is actually in, not TARGET's default. Without
    # this, an unqualified `here - --run` would read the focused cell and then
    # PATCH an id that doesn't exist in whichever notebook the fallback picked.
    global TARGET
    TARGET = info["notebook"]

    if args.source is None and not args.run:
        print(f"{DIM}{info['notebook']} · kernel {info.get('kernel_status')}{RESET}\n")
        show_cell(cell)
        if info.get("error"):
            print(f"{RED}this cell's last run failed — the traceback is above{RESET}")
        return

    if args.source is not None:
        cell = call(f"/api/cells/{cid}", "PATCH",
                    {"source": read_source(args.source), "undoable": True})
        if "error" in cell:
            sys.exit(cell["error"])
        print(f"replaced {cid} {DIM}(↶ Undo replace is now on the cell){RESET}")

    if cell.get("cell_type") == "code":
        show_outputs(call(f"/api/cells/{cid}/run", "POST", {}).get("outputs"))


def cmd_undo(args):
    cell = call(f"/api/cells/{args.cell_id}/undo", "POST", {})
    if "error" in cell:
        sys.exit(cell["error"])
    print(f"restored {args.cell_id}")
    show_cell(cell)


def cmd_delete(args):
    call(f"/api/cells/{args.cell_id}", "DELETE")
    print(f"deleted {args.cell_id}")


def cmd_clear(args):
    call("/api/clear-outputs", "POST", {})
    print("cleared outputs")


def cmd_restart(args):
    print("kernel:", call("/api/kernel/restart", "POST").get("status"))


def cmd_tabs(args):
    """What the browser has open, and which interpreter each notebook runs on.

    Grouped by session, because a notebook in a session you aren't looking at is
    still open with a live kernel — invisible in the browser, but `gusnb` can
    target it and should say it's there.
    """
    data = call("/api/tabs")
    primary = data.get("primary")
    mine = {t["path"] for t in data.get("tabs", [])}
    name = data.get("session_name")
    if name:
        print(f"{DIM}session: {name}{RESET}")

    def show(t):
        mark = "*" if t["path"] == primary else " "
        line = f"{mark} {t['kind']:<9} {t['path']}"
        if t["kind"] == "notebook":
            k = call("/api/kernel?notebook=" + urllib.parse.quote(t["path"]))
            line += f"\n            {DIM}{k.get('status')} · {k.get('python')}{RESET}"
        print(line)

    for t in data.get("tabs", []):
        show(t)
    elsewhere = [t for t in data.get("all_tabs", []) if t["path"] not in mine]
    if elsewhere:
        print(f"\n{DIM}open in other sessions:{RESET}")
        for t in elsewhere:
            show(t)


def cmd_open(args):
    """Open a notebook or text file as a tab in the browser."""
    path = target_path(args.path)
    data = call("/api/open", "POST", {"path": path})
    if "error" in data:
        sys.exit(data["error"])
    kind = data.get("kind", "text")
    extra = f" · {len(data['cells'])} cells" if kind == "notebook" else ""
    print(f"opened {kind}: {data.get('path', path)}{extra}")
    if kind == "notebook":
        print(f"{DIM}target it with: gusnb -n {args.path} add ...{RESET}")


def cmd_close(args):
    data = call("/api/close", "POST", {"path": target_path(args.path)})
    print("closed" if data.get("closed") else "was not open")


def cmd_env(args):
    """Show or set the interpreter this notebook's kernel runs on."""
    if not args.python:
        data = call("/api/venvs")
        print(f"{BOLD}current{RESET} {data['current']}")
        print(f"{DIM}{data['notebook']}{RESET}\n")
        for v in data.get("venvs", []):
            note = v.get("origin") or ""
            if not v.get("ipykernel"):
                note = (note + " · no ipykernel").strip(" ·")
            print(f"  {v['label']:<18} {v.get('version') or '?':<8} "
                  f"{DIM}{note}{RESET}\n    {DIM}{v['python']}{RESET}")
        return

    data = call("/api/venv", "POST", {"python": target_path(args.python)})
    if "error" in data:
        sys.exit(data["error"])
    print(f"kernel now on Python {data['version']} — {data['python']}")


def main():
    p = argparse.ArgumentParser(
        prog="gusnb",
        description="Drive GusNotebook's notebook pane from the terminal.")
    p.add_argument("-n", "--notebook", metavar="PATH",
                   help="which open notebook to act on (default: the first one)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show all cells and their outputs").set_defaults(fn=cmd_list)

    a = sub.add_parser("add", help="add a cell (code cells run unless --no-run)")
    a.add_argument("source", nargs="?", default="", help='source, or "-" for stdin')
    a.add_argument("--md", "--markdown", action="store_true", dest="md")
    a.add_argument("--raw", action="store_true")
    a.add_argument("--no-run", action="store_true")
    a.add_argument("--after", help="insert after this cell id")
    a.add_argument("--index", type=int, help="insert at this position")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("run", help="run one cell")
    r.add_argument("cell_id")
    r.add_argument("source", nargs="?", help='optional new source, or "-" for stdin')
    r.set_defaults(fn=cmd_run)

    sub.add_parser("run-all", help="run every code cell").set_defaults(fn=cmd_run_all)

    s = sub.add_parser("set", help="replace a cell's source")
    s.add_argument("cell_id")
    s.add_argument("source", help='new source, or "-" for stdin')
    s.add_argument("--run", action="store_true")
    s.set_defaults(fn=cmd_set)

    h = sub.add_parser("here", help="the cell the user is on: read, or replace and run")
    h.add_argument("source", nargs="?",
                   help='replace the cell with this, or "-" for stdin')
    h.add_argument("--run", action="store_true",
                   help="run it (implied when source is given)")
    h.set_defaults(fn=cmd_here)

    u = sub.add_parser("undo", help="put back the source a replace overwrote")
    u.add_argument("cell_id")
    u.set_defaults(fn=cmd_undo)

    d = sub.add_parser("delete", help="delete a cell")
    d.add_argument("cell_id")
    d.set_defaults(fn=cmd_delete)

    sub.add_parser("clear", help="clear all outputs").set_defaults(fn=cmd_clear)
    sub.add_parser("restart", help="restart the kernel").set_defaults(fn=cmd_restart)

    sub.add_parser("tabs", help="list open tabs and their kernels").set_defaults(fn=cmd_tabs)

    o = sub.add_parser("open", help="open a file as a tab in the browser")
    o.add_argument("path")
    o.set_defaults(fn=cmd_open)

    c = sub.add_parser("close", help="close a tab")
    c.add_argument("path")
    c.set_defaults(fn=cmd_close)

    e = sub.add_parser("env", help="show or set this notebook's Python")
    e.add_argument("python", nargs="?",
                   help="a virtualenv directory or python binary")
    e.set_defaults(fn=cmd_env)

    args = p.parse_args()
    if args.notebook:
        global TARGET
        TARGET = target_path(args.notebook)
    args.fn(args)


if __name__ == "__main__":
    main()
