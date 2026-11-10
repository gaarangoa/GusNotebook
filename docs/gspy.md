# Scientific notebooks (.gspy)

Create one with **+ → Scientific notebook**. Ordinary `.ipynb` notebooks still
use their existing interface. Both formats execute Python cells in document
order, sharing one kernel per document. Opening a document never executes it.

## Write directly in the document

`.gspy` uses **Quill** for a continuous rich-text document. Click the page and
write; use the compact toolbar for headings, bold, italic, and lists. Formatting
and prose autosave, with save status and retry controls at the bottom. Standard
undo/redo applies to document edits, including inserting or deleting Python.

Place the cursor between paragraphs, or within a paragraph, and choose
**+ Python**. Its inspector contains the editable Python source, **Run**,
**Run through here**, **Results**, **Copy result**, and **History**. Close the
inspector to continue writing. Click the small Python label to reopen it.

Choose one of two modes in the inspector:

- **Show output in document** renders the step's figures, tables, and text inline.
- **Background step** keeps only a compact marker in the document. Its execution
  output and errors remain available in the inspector, and its runs remain audited.

Both modes execute in the same ordered kernel. Choose the Python environment
using the existing kernel/environment control. Editing or opening the document
never runs Python automatically. **Run all** executes Python in document order.

## A live result sentence

Insert a Python step containing:

```python
performance = {"estimate": 0.25, "ci_low": 0.25, "ci_high": 0.26}
```

1. In its **Results** dialog, publish `performance`, save, and run the step.
2. After the step, type `The result is {{estimate}}, with CI {{low}}–{{high}}.`
3. With the cursor in that paragraph, choose **Live values**. Add names
   `estimate`, `low`, and `high`; choose the producing Python step,
   `performance` as the variable, and `estimate`, `ci_low`, or `ci_high` as
   the respective field path. Use `.2f` for two decimal places.
4. Save. Click a rendered value to inspect the exact source revision and run
   that supplied it. **Edit current code** opens the current editable source,
   which may differ from that producing revision.

A rerun refreshes the linked values while preserving your surrounding prose.
Live values refer to the same step or earlier steps; they do not evaluate code
while rendering. Inline code formatting keeps `{{name}}` literal.

## Figures and tables

Python initially displays all of its outputs. In **Results**, select individual
output placements and choose document flow, two columns, or a slide section.
These layouts apply to the output area; prose is written directly in Quill.
Layout and display-mode changes do not change Python execution order.

Give a figure or table a stable name using ordinary IPython display metadata:

```python
from IPython.display import display
display(fig, metadata={"gusnotebook": {"result": "pca"}})
```

Choose `pca` as the output name, or `*` for all outputs. **Source** opens the
producing revision. **Copy result** carries the result and its producing code
into the existing HTML/report workflow. HTML outputs remain isolated from the
editor in sandboxed frames.

## Runs, review, and history

**Run through here** executes earlier code blocks in order and stops at this
block. **More workspace actions → Run all blocks** executes the document in
order. The content view stops these
sequences on an execution error. Individual runs retain normal notebook
semantics; GusNotebook does not infer Python's dependency graph.

Published values are captured as part of the producing execution. Later code
reusing a variable name cannot change an earlier saved value. Changes to code,
published variables, execution order, earlier runs, or the kernel mark affected
results as outdated. Failed/interrupted reruns retain the last successful view
with a visible warning. Missing values are labeled; they never run code to
resolve themselves. A saved result opened without a live kernel is labeled
saved, rather than verified current.

When supporting runs change, place the cursor in the linked paragraph to review
the surrounding interpretation. **Mark reviewed** in the footer records the
displayed run IDs and content revision in the history.
The review records an explicit action, not a guarantee of scientific validity.

The Python inspector's **History** button shows that step's recorded
interactions. **More workspace actions → Document history** also retains deleted blocks and exports the full
`.history.jsonl` log. The `.gspy` itself stores revisions, artifacts, run starts,
completions, captured values, and reviews. It is the source of the exported log.
The existing block copy action also carries the successful producing run and
source into its provenance snapshot, even if the current code has changed.

If a completed run cannot be written, **Retry saving result** in the Python
inspector retries storage without rerunning Python. Pending output remains in this server process until
saved; restarting the server can lose that pending output. Its durable start
event remains and is shown as having no saved completion.

## Current boundaries

Prose is saved as sanitized HTML in ordinary Markdown cells, with a Quill Delta
in cell metadata for exact rich-text editing. Python stays in real code cells.
An agent editing a prose cell's source invalidates its old Delta so its new text
is rendered on reload. Prose saves preserve newer code and execution outputs;
conflicting prose/order edits retain the local draft and report a conflict.
Copy unsaved prose elsewhere before using **Reload** to discard that draft.
Older `.gspy` paragraphs remain editable; their first edit stores the rich-text
representation. Raw cells are shown as non-executable text.

Bindings are explicit declarations, not automatically inferred variable lineage.
Published values support finite JSON scalars, records, lists, and NumPy scalars;
each result is limited to 4,096 items, 12 nested levels, and 65,536 text
characters. Display large tables/plots as ordinary outputs instead. Layouts
remain inside ordered blocks, with no freeform canvas.

The existing agent CLI works for source edits and execution, preserving content
configuration when code changes. Component-aware agent tools, complete visible
agent-response capture, `.ipynb` import/export, report export with run links,
activity timing, and large-document benchmarks remain on the
[implementation plan](gspy-implementation-plan.md). History records what the
application observed; it cannot reconstruct interactions that were never captured.
