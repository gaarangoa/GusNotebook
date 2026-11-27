---
name: html-slide-deck
description: Create or extend HTML research slide decks using GusNotebook's scientific presentation template, with notebook-backed figures and tables.
---

# HTML slide decks

Use [assets/deck.html](assets/deck.html) for a new HTML presentation. Its design
comes from the user's `model-architecture.html`: white slides on a light gray
canvas, blue section labels, bold headings, compact cards, colored workflow
lanes, restrained tables, and a subtle footer. The asset contains reusable
layouts, not scientific findings. Read its CSS and markup before adapting it.

## Design and content

- Preserve an existing deck's design and content when extending it. Use this
  template for a new deck unless the user supplies another design.
- Keep the 1100 px desktop canvas, 720 px minimum slide height, generous slide
  padding, 28 px headings, and compact body text. Slides grow with content;
  do not set a fixed height or hide overflow. Keep the responsive and print CSS.
- Use blue `#2E6B9E` for the main accent, green `#3D8A5F`, orange `#C96B2E`,
  and purple `#8B5AA6` to distinguish meaningful groups or workflow branches.
  Keep the Inter/system sans-serif stack; no font download is required.
- Choose the layouts the story needs: overview with cards and a pipeline,
  architecture/method diagram, figure with commentary, results table, or
  data scope and limitations. Duplicate, reorder, or remove slides as needed.
  Five examples are not a required slide count. CSS numbers slides automatically.
- Write titles that express the slide's main point. Replace sample labels and
  empty slots with the user's content. Do not copy model names, measurements,
  dates, or findings from the original reference, or invent results to fill space.
- Use inline SVG or HTML for explanatory diagrams. Use responsive D3 outputs
  for computed figures unless the user asks for another library. Keep diagrams
  and tables editable rather than flattening the slide into an image.

## Notebook-backed content

When working in a linked GusNotebook project, read `gusnb project context` for
the report, notebook, approved data, and uv environment. New project reports
already use this template. Read the report and its revision before editing.

Keep Python data preparation and the complete D3/HTML rendering code in the
linked notebook, execute it, then export the producing cell with:

```bash
gusnb -n NOTEBOOK export-output CELL_ID --file FIGURE_HTML
```

Read the exported fragment from disk and insert it in the chosen slide. Replace
the whole `.figure-slot` placeholder; do not recreate the plot in the report or
put it inside another fixed-size iframe. Preserve the fragment's provenance,
code snapshot, and history. GusNotebook opens the generating cell through the
selected figure's **Source → Open notebook code** control; add no code button
or label to the figure itself. The same applies to computed tables
and metrics: use the executed notebook output, not the template's sample rows.
Apply the deck's palette and table typography inside the notebook HTML output
too: exported figures use isolated iframes and do not inherit the deck's CSS.
Keep explanatory text beside the figure when requested. Save through
`gusnb project report --file UPDATED_HTML --revision SAVED_REVISION` and merge
any concurrent changes. Follow the existing project instructions for data,
uv, notebook history, and Git.

For a standalone deck, copy this asset to the requested output file and edit
it there. It has no runtime dependencies. Do not create a notebook or a project
just to write a presentation that contains no computed analysis.

## Check the result

Inspect the rendered deck at desktop and narrow widths. Check long headings,
diagrams, table labels, figure dimensions, and the footer for overlap or clipping.
Use the print layout when preparing a PDF. Keep the notebook-source control
working in GusNotebook and leave existing provenance markers intact.
