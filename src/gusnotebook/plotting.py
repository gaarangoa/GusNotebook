"""Shared plotting guidance for notebook agents and starter snippets."""

D3_INSTRUCTIONS = r"""Notebook figures and plots — required default:
When asked to create a figure, plot, chart, or data visualization in a notebook,
start directly with a D3.js figure in the notebook cell. Use D3 unless the user
explicitly requests a different library or rendering method. Do not first make
a Matplotlib, Seaborn, Plotly, or other alternative figure, or ask which library
to use. Generic plotting examples and starter skills do not override this rule;
an explicit user request does.

GusNotebook bundles D3.js 7.9.0 locally and provides `window.d3` synchronously in
every notebook HTML output before the cell's scripts execute. Use Python's
`from IPython.display import HTML, display` and `display(HTML(...))` containing
the figure markup and an inline <script> that uses `d3`. No CDN, external script,
npm/pip install, or library download is needed. Keep data inline too: serialize
Python data with json.dumps(..., allow_nan=False).replace('<', '\\u003c') before
embedding it in a script. Use the user's actual data, label axes and units, and
give SVG figures a responsive viewBox. Draw the final figure immediately without
animation or transition timers. Write and run the notebook cell; do not leave
the figure code only in the terminal. Honor explicit requests for alternatives.
"""

D3_EXAMPLE = r'''```python
import json
from IPython.display import HTML, display

# Replace this example data and labels with the user's actual data.
data = [{"label": "A", "value": 18}, {"label": "B", "value": 32}, {"label": "C", "value": 24}]
payload = json.dumps(data, allow_nan=False).replace("<", "\\u003c")
display(HTML("""<div id="chart"></div><script>
(() => {
  const data = """ + payload + """;
  const width = 640, height = 320;
  const margin = {top: 36, right: 20, bottom: 48, left: 56};
  const svg = d3.select('#chart').append('svg')
    .attr('viewBox', `0 0 ${width} ${height}`).attr('role', 'img')
    .attr('aria-label', 'Values by category').style('width', '100%');
  svg.append('title').text('Values by category');
  const x = d3.scaleBand().domain(data.map(d => d.label))
    .range([margin.left, width - margin.right]).padding(0.25);
  const y = d3.scaleLinear().domain([0, d3.max(data, d => d.value) || 1])
    .nice().range([height - margin.bottom, margin.top]);
  svg.append('g').attr('transform', `translate(0,${height - margin.bottom})`)
    .call(d3.axisBottom(x));
  svg.append('g').attr('transform', `translate(${margin.left},0)`)
    .call(d3.axisLeft(y).ticks(5));
  svg.selectAll('rect.bar').data(data).join('rect').attr('class', 'bar')
    .attr('x', d => x(d.label)).attr('y', d => y(d.value))
    .attr('width', x.bandwidth()).attr('height', d => y(0) - y(d.value))
    .attr('fill', '#3978b5').append('title').text(d => `${d.label}: ${d.value}`);
  svg.append('text').attr('x', margin.left).attr('y', 20).text('Values by category');
  svg.append('text').attr('x', width / 2).attr('y', height - 6)
    .attr('text-anchor', 'middle').text('Category');
  svg.append('text').attr('transform', 'rotate(-90)').attr('x', -height / 2)
    .attr('y', 16).attr('text-anchor', 'middle').text('Value');
})();
</script>"""))
```'''
