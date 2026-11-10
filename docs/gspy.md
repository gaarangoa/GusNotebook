# Scientific notebooks (.gspy)

Create one with **+ → Scientific notebook**. Ordinary `.ipynb` notebooks still
use their existing interface. Both formats execute Python cells in document
order, sharing one kernel per document. Opening a document never executes it.

## A live result sentence

Add a code block, or ask your agent to create it using the existing `gusnb`
commands. For example:

```python
performance = {"estimate": 0.25, "ci_low": 0.25, "ci_high": 0.26}
```

In the block's **Content** editor:

1. Enter `performance` under **Publish Python variables**.
2. Add a paragraph containing `The result is {{estimate}}, with CI {{low}}–{{high}}.`
3. Add three live values named `estimate`, `low`, and `high`. Choose this block
   as producer, `performance` as variable, and `estimate`, `ci_low`, or `ci_high`
   as the respective field path. Use `.2f` for two decimal places.
4. Save content, then **Run**. Click a rendered value to inspect the exact
   source revision and run that supplied it. **Edit current code** opens the
   current editable source, which may differ from that producing revision.

Markdown cells support the same references through **Values**. Double-click
their prose to edit the Markdown. References use `{{name}}`; code spans and
code fences stay literal. Surrounding prose is never rewritten by a rerun.

## Figures, tables, and layouts

Code blocks initially display all their outputs. In **Content**, add, reorder,
or remove paragraphs and output placements; select document flow, two columns,
or a slide section. Layout changes do not change Python execution order.

Give a figure or table a stable name using ordinary IPython display metadata:

```python
from IPython.display import display
display(fig, metadata={"gusnotebook": {"result": "pca"}})
```

Choose `pca` as an output placement's name. `*` shows all outputs. A placement
or value can refer to its own block or an earlier code block. **Source** opens
the producing revision; **Code** reveals the block's current source. Existing
HTML isolation, image rendering, and table rendering also apply here.

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

When supporting runs change, review the surrounding interpretation. **Mark
reviewed** records the displayed run IDs and content revision in the history.
The review records an explicit action, not a guarantee of scientific validity.

The cell's History button shows its recorded interactions. **More workspace
actions → Document history** also retains deleted blocks and exports the full
`.history.jsonl` log. The `.gspy` itself stores revisions, artifacts, run starts,
completions, captured values, and reviews. It is the source of the exported log.
The existing block copy action also carries the successful producing run and
source into its provenance snapshot, even if the current code has changed.

If a completed run cannot be written, **Retry saving result** retries storage
without rerunning Python. Pending output remains in this server process until
saved; restarting the server can lose that pending output. Its durable start
event remains and is shown as having no saved completion.

## Current boundaries

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
