# GSPY implementation plan

Status: phase 1 implemented; phases 2–6 remain planned. The original design
was based on the repository at `64593f7`.

The format/history foundation supports creating `.gspy` files beside `.ipynb`,
existing ordered execution and CLI operations, document-wide history,
recoverable deleted blocks, historical output snapshots, and a self-contained
JSONL log export. File discovery, rename/copy identity, external reloads, and
session restoration are integrated. The initial interface uses the existing
notebook renderer. Live value bindings, the content-oriented renderer, richer
agent capture, conversion/export, and activity tracking are subsequent phases.

## Product contract

GusNotebook will support two notebook experiences side by side:

| Format | Experience | Execution |
| --- | --- | --- |
| `.ipynb` | Existing notebook interface and Jupyter compatibility | Existing ordered cells and shared Python state |
| `.gspy` | A scientific document with executable sections, live values, figures, tables, and optional slide layouts | The same ordered cells and shared Python state |

A GSPY block is still a notebook cell. A code block can render a complete section:
introductory text, a result sentence, a figure, a table, and a caption. Its source
is initially collapsed and can be opened, edited, and rerun. Markdown blocks
remain available for narrative that needs no executable source.

Document order is execution order for Run all. Users can also run individual
blocks, as they can in a notebook today. Moving an entire block changes that
order; arranging content inside a block does not. There is no separate workflow
engine, dependency scheduler, or automatic execution on opening a document.

Normal notebooks remain usable alongside GSPY documents, including in the same
session. Conversion creates a new file and never silently changes an existing
notebook's format.

## Repository findings

| Existing component | Reuse and required work |
| --- | --- |
| [`notebook.py`](../src/gusnotebook/notebook.py) | Stable cell IDs, nbformat storage, external reloads, editing, and saved outputs. Add a format adapter and GSPY metadata validation. |
| [`kernel.py`](../src/gusnotebook/kernel.py) and [`app.py`](../src/gusnotebook/app.py) | Per-document kernels, serialized execution, streaming, interruption, and run requests. Persist execution identities and capture declared results within the execution lock. |
| [`cellhistory.py`](../src/gusnotebook/cellhistory.py) | Requests, source revisions, output digests, and HTML provenance links. Extend this vocabulary into document history with retained output versions. |
| [`history.py`](../src/gusnotebook/history.py) | Workspace undo recordings, limited to 20 groups and 10 MB per group. Keep these separate from the durable GSPY history. |
| [`persistence.py`](../src/gusnotebook/persistence.py) | Atomic saves, permissions, and stale-write checks. Save content changes and their history together. |
| [`cells.js`](../src/gusnotebook/static/js/cells.js), [`actions.js`](../src/gusnotebook/static/js/actions.js), [`events.js`](../src/gusnotebook/static/js/events.js) | Output renderers, editors, drafts, execution controls, and reconnect handling. Share these services with a separate GSPY renderer. |
| [`cli.py`](../src/gusnotebook/cli.py) and [`terminals.py`](../src/gusnotebook/terminals.py) | Agent targeting, focused-cell context, and prompt hooks. Extend selection context to content components and use explicit request IDs for new actions. |

Current cell history travels in cell metadata, but deleting the cell removes
that attached history. Run events retain source and an output digest, not every
historical output. Current prompt hooks are not a complete record of agent
responses. These are specific gaps the new history design must address.

## Storage and identities

Use a self-contained JSON file for the first format version. Retain the
nbformat v4 core (`cells`, `metadata`, and standard output MIME bundles), with
GSPY extensions under a versioned `metadata.gusnotebook` namespace. Validate both
the nbformat core and the GSPY schema. Opening an unsupported version must
preserve the file and report the incompatibility.

This allows the existing document and kernel services to work with both
formats. Internally, both tabs can remain `kind: notebook`, with an additional
`format: ipynb | gspy` field selecting the renderer and persistence behavior.
Centralize extension checks instead of adding scattered `.gspy` conditionals.

The proposed data model contains:

| Record | Purpose |
| --- | --- |
| Document ID and schema version | Stable identity across renames and explicit format migrations |
| Ordered cells with stable IDs | Executable source or Markdown, plus ordinary notebook outputs |
| Presentation components | Paragraphs, output placements, captions, and simple layouts, each with an ID |
| Result declarations and bindings | A producer block, named result, optional field path, and display formatting |
| Run records | Run ID, kernel generation, executed source revision, timestamps, execution count, status, declared inputs, and captured results |
| Revision and artifact records | Recoverable source/content versions and historical MIME outputs, deduplicated by content hash |
| Document event journal | Chronological interactions and changes, including events for deleted blocks |

Keep presentation metadata with its cell and the journal/run/artifact records
at document level. Cell history is a filtered view of that journal, not a second
independently maintained event list. Restoring a version creates a new event.
Deletion retains the block's last revision and all of its history.

Rename preserves identity. An explicit duplicate/import operation creates a
new document ID and records its origin. Paths remain useful location hints, but
bindings must not depend on a filename or a cell's numerical position.

The first version embeds artifacts so moving the `.gspy` file carries its
history. Deduplicate repeated snapshots and do not store a complete output copy
for every streamed chunk. History has no silent age/count pruning. Benchmark
file growth and save latency before release; if embedded artifacts fail the
agreed scale checks, revise the storage design before declaring the format
stable. Do not solve growth by silently discarding old results.

## Live text and figures

For the performance example, a producer block calculates a structured result:

```python
performance = {"estimate": 0.25, "ci_low": 0.25, "ci_high": 0.26}
```

Its presentation contains a paragraph with three typed value references. The
editor displays:

> The performance was **0.25**, with a confidence interval of **0.25–0.26**.

Each reference identifies the producer block, result name `performance`, field,
and formatting such as two decimal places. Users edit the surrounding sentence
normally. The reference is a distinct editable component, so deleting it is an
explicit unlink rather than an accidental change to Python code.

Selecting a bound value opens the producing block's code and the run that
supplied that value. If its current source has changed, make the difference
between the producing revision and the current editable revision visible.

For PCA, the block produces an ordinary figure output with an explicit name,
such as `pca`. A section places a paragraph, that named figure, and its caption.
Selecting the figure exposes compact Show code, Run, and History controls.
Use MIME metadata to name figure/table outputs where possible, preserving their
ordinary notebook representation. Do not depend solely on output position,
which changes when an agent adds another display or print statement.

Binding rules:

- Capture explicitly declared values immediately after successful code
  execution, under the same kernel lock and run identity. A later cell that
  reuses the variable name must not change an earlier block's stored result.
- Start with JSON-compatible scalars/records and standard display outputs.
  Resolve value selectors in the kernel execution path; never evaluate Python
  from the browser renderer or while opening a saved document.
- Treat bindings as explicit provenance declarations. Do not claim that a
  variable name alone proves where every value was originally calculated.
- References may use the same block or a previously produced result. Unresolved
  references display a clear missing/unexecuted state and never trigger runs.
- Successful reruns update the current bound values and outputs together;
  historical versions remain available. Failed or interrupted runs retain the
  previous successful view with a visible failure/stale indicator.
- Code edits, block reordering, kernel restarts, and upstream reruns invalidate
  affected results. Initially mark later executable blocks conservatively;
  do not infer a complete Python dependency graph.
- Retain notebook semantics for individual runs: show actual execution order,
  and mark results as potentially stale when earlier blocks were skipped or
  changed. Offer Run through here and Run all for ordered refreshes.
- Changes to supporting results mark manually written interpretation for
  review. Only the bound values and output placements update automatically.

## History and the separate log

The `.gspy` journal is the source record. Export it as
`<document-name>.history.jsonl`, with a readable history view in the application.
The log is generated from a consistent saved revision; it is not a second
database that can drift from the notebook.

Every event has a unique ID, document sequence number, timestamp, actor/source,
affected block/component IDs, and relevant request, revision, and run IDs.
Sequence numbers preserve committed order even when client clocks differ.
Retries use operation IDs so reconnecting does not duplicate events.

Record the following:

- Submitted requests, captured visible agent responses, corrections, explicit
  acceptance/rejection, and decision notes. An applied edit is not automatically
  evidence that a human reviewed or accepted it.
- Text, code, binding, and layout changes, including creations, deletions,
  moves, restores, and external edits observed by the app.
- Agent preferences/instructions in effect for an interaction, with their
  version and source. Deduplicate unchanged instruction snapshots.
- Run start, finish, failure, cancellation, source revision, captured values,
  output versions, and environment/input information available to the app.
- Copies and exports, including source run/artifact and destination when known.
  A successful clipboard write and an observed save into HTML are separate
  events, as they are in the existing provenance design.
- Activity intervals for narrative editing, code editing, requesting agent
  work, reviewing content, and execution/agent waiting.

Store recoverable before/after revisions or reconstructable patches, rather
than only summaries. Preserve full captured messages; shorten only their UI
previews. Import existing cell histories with their original timestamps and
attribution. Mark missing historical data as unavailable instead of inventing
earlier events or reconstructing past outputs from hashes.

Saving an edit and its journal event is one atomic document transaction. If
saving fails, do not acknowledge a durable edit or append a false success
receipt. Execution cannot roll back Python side effects: if a completed run
cannot be saved, retain its pending result for retry and visibly report the
save failure. Persist a run-start record before dispatch and mark unfinished
runs interrupted/unknown after recovery, rather than inventing completion.

External edits are observed changes, not proof of which process made them.
Changes made while the app is closed cannot yield an invented interaction
transcript. Prompt hooks currently capture only part of an agent session;
structured adapters must capture visible response/action events where the
agent exposes them. Record coverage gaps explicitly. Do not use raw terminal
screen redraws as the authoritative conversation history.

Activity recording measures focused interaction intervals, with an idle cutoff
and cross-tab deduplication. Execution and agent waiting can overlap human work
and must be reported separately. Reading/thinking time cannot be inferred
precisely from input activity. Do not record every keystroke or treat an open tab
as uninterrupted active work.

The JSONL export includes revision data and references to run/output artifacts.
Provide an option to bundle referenced artifacts alongside the log for a
self-contained audit export; a reference-only log must identify the source
document revision required to inspect those outputs. Generate readable
summaries from the same events without replacing their underlying records.

## Interface and agent integration

Add **Scientific notebook (.gspy)** to the existing New menu, alongside the
current notebook option. Start with a blank narrative block and compact ways
to add narrative or executable content.

Use a dedicated GSPY rendering module and scoped styles. Reuse Markdown/math,
MIME output renderers, CodeMirror, saving, streaming, and interruption. Preserve
ordinary Markdown heading sizes, the compact toolbar, dark/light themes, and
the existing notebook clipboard behavior.

The first layouts are flowing content, a two-column section, and a slide-sized
section. All remain within ordered blocks. A freeform slide canvas, complex
animation, and a new rich-text framework are outside the initial release.
HTML and interactive outputs must use the existing isolation and sanitization
boundaries; layout metadata cannot inject privileged scripts into the editor.

Code is revealed through the selected content. A run failure remains visible
even when code is collapsed. Keep History as a compact selected-block action
and provide a document-wide history panel with filters and log export. Do not
add a permanent history banner to every section.

Extend focused context to include document, block, component, binding, displayed
run, and selected text. Extend `gusnb` with structured read/update operations
for presentation and bindings, while keeping existing list/add/set/run commands
working for both formats. New mutations carry explicit request IDs and expected
revisions, preventing a delayed agent action from targeting another selection
or overwriting a newer user edit. An agent response can propose or apply edits
under the current execution permissions; it must not silently discard narrative
or reset presentation whenever code is regenerated.

## Import and export

Import `.ipynb` by creating a new `.gspy` file with the same ordered cells,
sources, saved outputs, Markdown, and available history. Give imported code
blocks a default presentation that displays their outputs in order.

Export a standard `.ipynb` using normal Markdown/code cells and MIME outputs:

- Preserve executable source order and environment metadata.
- Flatten presentation in reading order. Turn narrative components into
  Markdown and result placements into standard saved output bundles, retaining
  the producing code cell. Split a presentation into ordinary Markdown and
  output-only code cells where interleaving requires it; those generated cells
  contain no additional executable source and carry their origin in metadata.
- Resolve bound inline values to their current saved text. Mark missing/stale
  values honestly. Do not run code merely to export a document.
- Omit positioning, column layout, and slide sizing. Preserve ordinary
  Markdown structure, including headings and captions.
- Keep GusNotebook provenance in optional metadata or the companion history
  export. Standard notebook viewers do not need GSPY-specific code to open it.

The export must open and validate as nbformat v4, and its original code must run
in the appropriate Python environment. In a standard viewer, rerunning updates
the ordinary code outputs; flattened narrative values and extra presentation
snapshots remain static. Document that limitation rather than implying live
GSPY bindings work in every notebook viewer.

Reuse the existing HTML provenance snapshot mechanism for HTML/slides. Exported
content links to the exact producing run and artifact, even if the source
document is later rerun. Record successful exports only after the destination
has been written successfully.

## Delivery sequence and acceptance gates

1. **Format and durable history foundation.** Add GSPY schema/adapter, document
   IDs, journal, revisions, artifact records, and log export. Integrate file
   recognition, creation, rename, session restoration, watchers, and CLI
   targeting. Gate: `.ipynb` and `.gspy` open in one session; edits, moves,
   deletion, and restoration retain history across restart; corrupt and stale
   writes preserve the original file.

2. **Execution and result bindings.** Persist run IDs/kernel generations,
   capture named values and display outputs, and implement result validity
   states. Gate: the performance example updates only from its producer;
   subsequent variable reuse cannot change it; failed/interrupted runs, kernel
   restarts, out-of-order execution, and edits during execution cannot silently
   relabel old outputs as new results.

3. **Content rendering and inspection.** Add the GSPY renderer, editable
   narrative, embedded references, simple layouts, code disclosure, contextual
   run controls, and history views. Gate: create the performance sentence and
   PCA section, inspect both producers, rerun them, retain surrounding text,
   and reopen the saved document with the same content and layout.

4. **Agent actions and interaction capture.** Add component-aware context,
   structured CLI/API mutations, request/response adapters, instruction versions,
   and explicit review events. Gate: a request and follow-up correction link
   to their exact revisions and runs; retargeting, concurrent edits, and retries
   cannot attach an action to the wrong content; unsupported transcript capture
   is visibly identified.

5. **Interoperability and report provenance.** Add import, flattened `.ipynb`
   export, HTML/slides export links, and complete log/artifact export. Gate:
   exported fixtures validate with nbformat, execute in order without the GSPY
   UI, and preserve saved figures/text; exported history reconstructs deleted
   and revised content. The source `.ipynb` remains unchanged during import.

6. **Activity records, scale, and release checks.** Add active/idle activity
   intervals and reporting. Exercise large histories, large plots, bounded live
   updates, disconnected/reconnected clients, and remote sessions. Page history
   APIs and render only requested historical output versions. Measure open/save
   latency and memory with representative research documents before finalizing
   the storage format. Gate: idle tabs do not accumulate active work; multiple
   tabs do not double-count it; saved history survives disconnect and restart.

The first usable demonstration is phases 1–3. The full agreed first release
includes all six phases. Agent training, automatic learning of preferences,
automatic dependency scheduling, collaborative multi-user editing, and an
arbitrary freeform slide designer are future work.

## Validation plan

Use isolated app/kernel/browser fixtures, never a user's live workspace.

- Unit tests: schema round trips and migrations; stable IDs; revision/artifact
  retention; atomic content-plus-history writes; conflict handling; log order
  and deduplication; history after deletion; bindings and export flattening.
- Real kernel integration: sequential shared state; run identity; scalar
  capture; named PCA output; failed and canceled execution; output/source
  mismatch; kernel restart; variable reuse; Run all and Run through here.
- Browser tests: mixed-format tabs; narrative editing without losing bindings;
  click value/figure to inspect code; keyboard access; selected history action;
  dark/light rendering; persistence of layouts; pending drafts during streaming;
  reconnect without duplicate events or rerunning code.
- Export tests: nbformat validation, execution of original sources in a fresh
  kernel, static narrative values, preserved figures and captions, and exact
  run/artifact links in HTML and log exports.
- Regression tests: existing notebook editing/execution, cell history,
  Markdown/math, notebook clipboard and HTML image/provenance workflows,
  environment selection, terminal agent targeting, and tunnel reconnects.

Add the new meaningful suites to the existing CI workflow. Each implementation
phase should be independently reviewable and committed with its relevant
validation. The first implementation task is the format/history foundation,
not replacing the existing notebook renderer wholesale.
