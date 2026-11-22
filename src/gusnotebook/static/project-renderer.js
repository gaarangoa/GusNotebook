/* One renderer for report and notebook; scientific calculations stay in Python. */
document.querySelectorAll('.analysis-result').forEach(section => {
  const result = JSON.parse(section.querySelector('.analysis-data').textContent);
  const root = section.querySelector('.analysis-content');
  const add = (tag, text, parent = root) => {
    const el = document.createElement(tag); el.textContent = text; parent.append(el); return el;
  };
  add('h2', result.title || 'Analysis');
  add('p', result.summary || '');
  const metrics = add('div', ''); metrics.className = 'analysis-metrics';
  Object.entries(result.metrics || {}).forEach(([name, value]) => {
    const item = add('div', '', metrics); add('span', name, item);
    add('strong', typeof value === 'number' ? Number(value.toPrecision(6)) : value, item);
  });
  if (result.chart?.points?.length) {
    const chart = result.chart, width = 900, height = 500;
    const svg = d3.select(root).append('svg').attr('viewBox', `0 0 ${width} ${height}`)
      .attr('role', 'img').attr('aria-label', `${chart.x_label} versus ${chart.y_label}`);
    const x = d3.scaleLinear().domain(d3.extent(chart.points, d => d.x)).nice().range([65, width - 25]);
    const y = d3.scaleLinear().domain(d3.extent(chart.points, d => d.y)).nice().range([height - 55, 25]);
    svg.append('g').attr('transform', `translate(0,${height - 55})`).call(d3.axisBottom(x));
    svg.append('g').attr('transform', 'translate(65,0)').call(d3.axisLeft(y));
    svg.append('text').attr('x', width / 2).attr('y', height - 8).text(chart.x_label || 'x');
    svg.append('text').attr('transform', 'translate(16,250) rotate(-90)').text(chart.y_label || 'y');
    svg.append('g').selectAll('circle').data(chart.points).join('circle')
      .attr('cx', d => x(d.x)).attr('cy', d => y(d.y)).attr('r', 4).attr('fill', '#2563a5')
      .attr('opacity', .75).append('title').text(d => `${d.label || ''}: ${d.x}, ${d.y}`);
  }
  if (result.table) {
    const table = add('table', ''), header = add('tr', '', table);
    result.table.columns.forEach(column => add('th', column, header));
    result.table.rows.slice(0, 100).forEach(row => {
      const tr = add('tr', '', table); row.forEach(value => add('td', value, tr));
    });
    if (result.table.rows.length > 100) add('p', 'First 100 rows shown. Full results are in the run artifact.');
  }
  section.querySelector('.analysis-link')?.addEventListener('click', () => {
    parent.postMessage({type: 'gusproject-analysis', analysis: section.dataset.analysis, run: section.dataset.run}, '*');
  });
});
