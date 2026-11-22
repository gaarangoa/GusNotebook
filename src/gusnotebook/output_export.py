"""Export an executed notebook output with its producing code and history."""

from datetime import datetime, timezone
import html
import json
from pathlib import Path
import uuid

from . import cellhistory
from .project_templates import d3_source


def snapshot(notebook, cell, events, output_index=0):
    """Refuse stale outputs instead of attributing them to newly edited code."""
    outputs = cell.get("outputs", [])
    run = next((event for event in reversed(events) if event.get("kind") == "run"), None)
    if (not run or run.get("source") != cell.get("source") or
            run.get("output", {}).get("sha256") != cellhistory.output_summary(outputs)["sha256"]):
        raise ValueError("Run this cell in GusNotebook before exporting: its output must match its code.")
    if any(output.get("output_type") == "error" for output in outputs):
        raise ValueError("Fix the cell's execution error and rerun it before exporting.")
    rich = [output["data"] for output in outputs
            if output.get("output_type") in {"display_data", "execute_result"} and output.get("data")]
    if not 0 <= output_index < len(rich):
        raise ValueError(f"No displayed output at index {output_index}; this cell has {len(rich)}.")
    data = rich[output_index]
    mime = next((mime for mime in ("text/html", "image/svg+xml", "image/png", "image/jpeg", "text/plain")
                 if mime in data), None)
    if not mime:
        raise ValueError("This output has no HTML, SVG, image or text representation.")
    source = data[mime]
    source = "".join(source) if isinstance(source, list) else str(source)
    return {
        "kind": "gusnotebook-provenance-snapshot", "version": 3,
        "snapshot_id": uuid.uuid4().hex, "notebook": notebook,
        "notebook_name": Path(notebook).name, "cell_id": cell["id"],
        "execution_count": cell.get("execution_count"), "timestamp": datetime.now(timezone.utc).isoformat(),
        "code": cell["source"], "history": events, "output_mime": mime,
        "output_source": source, "output_index": output_index, "attachments": [],
    }


def render(payload):
    """Self-contained figure, using the existing HTML editor provenance format.

    Authored scripts stay in an opaque iframe, just like copied notebook plots.
    D3 and data travel with the output, so a saved report works offline.
    """
    mime, source = payload["output_mime"], payload["output_source"]
    if mime in {"image/png", "image/jpeg"}:
        body = f'<img alt="Notebook output" src="data:{mime};base64,{html.escape(source, quote=True)}">'
    elif mime == "text/plain":
        body = "<pre>" + html.escape(source) + "</pre>"
    else:
        body = source
    frame_id = "gusnb-viz-" + payload["snapshot_id"]
    runtime = FRAME_RUNTIME.replace("GUS_FRAME_ID", json.dumps(frame_id))
    document = ('<!doctype html><html><head><meta charset="utf-8">'
                '<style>html,body{margin:0;padding:0;font:10px/1.5 system-ui;color:#202632;background:white}'
                'img,svg,canvas{max-width:100%}pre{white-space:pre-wrap}table{border-collapse:collapse}'
                'td,th{padding:6px 10px;border-bottom:1px solid #ddd}</style>'
                '<script>' + d3_source().replace('</script', '<\\/script') + '</script></head><body>' +
                body + '<script data-gusnb-frame-runtime>' + runtime + '</script></body></html>')
    escape = lambda value: html.escape(str(value), quote=True)
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return (f'<figure class="gusnb-viz" data-gusnb-viz="1" data-gusnb-provenance="1" '
            f'data-gusnb-notebook="{escape(payload["notebook"])}" data-gusnb-cell-id="{escape(payload["cell_id"])}" '
            f'data-gusnb-snapshot="{escape(payload["snapshot_id"])}" style="margin:0">'
            '<div class="gusnb-viz-render" data-gusnb-viz-render>'
            f'<iframe class="gusnb-viz-frame" title="Notebook visualization" sandbox="allow-scripts" '
            f'referrerpolicy="no-referrer" data-output-frame="{frame_id}" data-gusnb-scale="1" '
            f'data-gusnb-base-height="460" height="460" style="display:block;width:100%;height:460px;border:0" '
            f'srcdoc="{escape(document)}"></iframe></div>'
            '<button type="button" data-gusnb-notebook-link contenteditable="false">Notebook code</button>'
            '<pre data-gusnb-viz-panel contenteditable="false" hidden>' +
            escape(payload["notebook"] + "\n\n" + payload["code"]) + '</pre>'
            '<script type="application/json" data-gusnb-viz-source>' + encoded + '</script></figure>' +
            '<script data-gusnb-viz-runtime>' + RESIZE_RUNTIME + '</script>')


FRAME_RUNTIME = """(() => {
  const id = GUS_FRAME_ID;
  let pending = false, previous = 0, dragging = false;
  const send = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      const nodes = [...document.body.children].filter(el => !el.matches('script,style,link'));
      const height = Math.ceil(Math.max(24, ...nodes.map(el => el.getBoundingClientRect().bottom + scrollY)));
      if (height === previous) return;
      previous = height;
      parent.postMessage({type:'gusnotebook-html-output-height', id, height}, '*');
    });
  };
  addEventListener('load', send); addEventListener('resize', send);
  new ResizeObserver(send).observe(document.body);
  addEventListener('message', event => {
    if (event.source === parent && event.data?.type === 'gusnotebook-viz-report-height') { previous = 0; send(); }
  });
  const pointer = (phase, event) => parent.postMessage({type:'gusnotebook-viz-pointer',
    id, phase, x:event.clientX, y:event.clientY}, '*');
  document.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    dragging = true; pointer('down', event);
  });
  document.addEventListener('pointermove', event => { if (dragging) pointer('move', event); });
  for (const name of ['pointerup', 'pointercancel']) document.addEventListener(name, event => {
    if (dragging) pointer('up', event); dragging = false;
  });
  send();
})();"""

RESIZE_RUNTIME = """(() => {
  if (window.__gusnbExportResize) return;
  window.__gusnbExportResize = true;
  addEventListener('message', event => {
    if (event.data?.type !== 'gusnotebook-html-output-height') return;
    const frame = [...document.querySelectorAll('iframe[data-output-frame]')].find(frame =>
      frame.contentWindow === event.source && frame.dataset.outputFrame === event.data.id);
    if (!frame || !Number.isFinite(event.data.height)) return;
    const height = Math.max(24, Math.min(20000, event.data.height));
    frame.style.height = height + 'px'; frame.height = height; frame.dataset.gusnbBaseHeight = height;
    const scale = Math.max(.35, Math.min(1.8, Number(frame.dataset.gusnbScale) || 1));
    const render = frame.closest('[data-gusnb-viz-render]');
    if (render) render.style.height = Math.round(height * scale) + 'px';
  });
})();"""
