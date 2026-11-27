"""Portable dataset lineage and a readable catalog; data files stay in place."""

from copy import deepcopy
import html
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

from .project_files import ProjectError, digest, encoded, read_file, write_file
from .project_layout import paths as layout_paths
from .project_sources import inspect_source, public_source, verify_reference
from . import project_access

START = '<!-- gusnb:data:start -->'
END = '<!-- gusnb:data:end -->'


def text(value, field, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ProjectError(field + ' must be nonempty text under ' + str(limit) + ' characters')
    return value.strip()


def project_path(root, value):
    value = text(value, 'File path', 4096)
    path = Path(value).expanduser()
    path = Path(os.path.abspath(path if path.is_absolute() else root / path))
    if not path.is_relative_to(root):
        raise ProjectError('Processed data and producing code must be inside the project')
    return path


def producer_info(project, spec):
    if not isinstance(spec, dict):
        raise ProjectError('Provide producer.path and, for a notebook, producer.cell_id')
    root = Path(project['path'])
    path = project_path(root, spec.get('path'))
    code_roots = [root / 'code', root / layout_paths(project)['notebooks']]
    if not any(path.is_relative_to(folder) for folder in code_roots) or path.suffix not in {'.py', '.ipynb'}:
        raise ProjectError('Producing code must be a Python file or notebook in the project code/notebook folders')
    raw = read_file(path, 32 * 1024 * 1024)
    result = dict(path=path.relative_to(root).as_posix(), execution_observed=False)
    if path.suffix == '.ipynb':
        cell_id = text(spec.get('cell_id'), 'Notebook cell ID', 200)
        notebook = json.loads(raw)
        if (not isinstance(notebook, dict) or not isinstance(notebook.get('cells'), list)
                or not all(isinstance(c, dict) for c in notebook['cells'])):
            raise ProjectError('Producer notebook is not a valid notebook document')
        def cell_source(cell):
            source = cell.get('source', '')
            if isinstance(source, list) and all(isinstance(part, str) for part in source):
                source = ''.join(source)
            if not isinstance(source, str):
                raise ProjectError('Producer notebook contains an invalid cell source')
            return source
        cell = next((c for c in notebook.get('cells', []) if c.get('id') == cell_id), None)
        if not cell or cell.get('cell_type') != 'code':
            raise ProjectError('Producer cell must be an existing code cell')
        source = cell_source(cell)
        from .cellhistory import entries
        if not isinstance(cell.get('metadata', {}), dict):
            raise ProjectError('Producer notebook contains invalid cell metadata')
        run = next((e for e in reversed(entries(cell)) if e.get('kind') == 'run'), None)
        if (run and run.get('source') == source and isinstance(run.get('output'), dict)
                and run['output'].get('errors') == []):
            result.update(execution_observed=True, execution={key: run.get(key) for key in ('id', 'at', 'execution_count')})
        result.update(cell_id=cell_id, source_sha256=digest(source.encode()))
        result['notebook_code_sha256'] = digest(encoded([
            {'id': c.get('id'), 'source': cell_source(c)} for c in notebook['cells']
            if c.get('cell_type') == 'code']))
    else:
        if spec.get('cell_id'):
            raise ProjectError('Cell IDs apply only to notebook producers')
        result['source_sha256'] = digest(raw)
    # Git is optional. A commit alone must never imply that uncommitted code
    # was part of it; retain the code fingerprint and explicit worktree status.
    try:
        commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=3)
        status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--ignored', '--', str(path)],
                                capture_output=True, text=True, timeout=3)
        if commit.returncode == status.returncode == 0:
            result['git'] = dict(commit=commit.stdout.strip(), uncommitted=bool(status.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def record(project, spec, timestamp, context):
    if not isinstance(spec, dict):
        raise ProjectError('Expected processed-data metadata as a JSON object')
    root = Path(project['path'])
    path = project_path(root, spec.get('path'))
    if not path.is_relative_to(root / layout_paths(project)['processed']):
        raise ProjectError('Save processed datasets under data/processed/ before recording them')
    output = inspect_source(path)
    relative = path.relative_to(root).as_posix()
    description = text(spec.get('description'), 'Dataset description')
    transformation = text(spec.get('transformation'), 'Transformation description')
    parameters = spec.get('parameters', {})
    if not isinstance(parameters, dict) or len(encoded(parameters)) > 20000:
        raise ProjectError('Parameters must be a JSON object under 20 KB')
    inputs = spec.get('inputs')
    synthetic = spec.get('synthetic', False)
    if type(synthetic) is not bool or not isinstance(inputs, list) or len(inputs) > 100 or not all(isinstance(v, str) for v in inputs):
        raise ProjectError('Provide inputs as a list of approved source labels or dataset paths')
    if not inputs and not synthetic:
        raise ProjectError('List the input datasets, or explicitly mark user-requested synthetic data')
    raw = [public_source(s, root) for s in project['sources']]
    processed = project.get('processed_data', [])
    if any(Path(s['path']) == path for s in raw):
        raise ProjectError('A processed output cannot overwrite a registered raw input')
    parents = []
    policy = project_access.load(project)
    for value in inputs:
        matches = [('raw', s) for s in raw if value in {s['id'], s['path']}]
        matches += [('processed', s) for s in processed if value in {s['path'], str(root / s['path'])}]
        if len(matches) != 1:
            raise ProjectError('Input is not uniquely registered in this project: ' + value)
        kind, parent = matches[0]
        actual = parent['path'] if kind == 'raw' else str(root / parent['path'])
        if kind == 'raw':
            project_access.require(project, actual, policy)
        if Path(actual) == path:
            raise ProjectError('An output cannot be its own input; retain the upstream dataset')
        verify_reference({**parent, 'path': actual})
        if 'file_revision' not in parent:
            parent = {**parent, 'file_revision': inspect_source(actual, schema=False)['file_revision']}
        parents.append({**deepcopy(parent), 'kind': kind})
    if len({(p['kind'], p['path']) for p in parents}) != len(parents):
        raise ProjectError('List each input dataset only once')
    # Traverse current processed parents to prevent circular lineage.
    by_path = {s['path']: s for s in processed}
    pending = [p['path'] for p in parents if p['kind'] == 'processed']
    seen = set()
    while pending:
        current = pending.pop()
        if current == relative:
            raise ProjectError('This update would create circular dataset lineage')
        if current in seen:
            continue
        seen.add(current)
        pending.extend(p['path'] for p in by_path.get(current, {}).get('inputs', []) if p['kind'] == 'processed')
    # Store only each parent's identity/revision, not its nested lineage tree.
    parents = [{k: p[k] for k in ('kind', 'id', 'path', 'file_revision', 'sha256', 'recorded_at') if k in p} for p in parents]
    producer = producer_info(project, spec.get('producer'))
    environment = project.get('environment') or {}
    return dict(path=relative, size=output['size'], schema=output['schema'], file_revision=output['file_revision'],
                description=description, transformation=transformation, parameters=deepcopy(parameters),
                inputs=parents, producer=producer, synthetic=synthetic, recorded_at=timestamp,
                access_revision=policy['revision'],
                activity={k: context[k] for k in ('actor', 'session', 'terminal', 'agent_session', 'request_id') if k in context},
                environment={k: environment[k] for k in ('python', 'lock_sha256', 'specification_sha256') if k in environment})


def catalog(project):
    paths = layout_paths(project)
    policy = project_access.status(project)
    sources = [public_source(s, project['path']) for s in project['sources']]
    return dict(format='gusnotebook-data-lineage', version=1, project=project['name'],
                raw_inputs=[s for s in sources if project_access.allows(policy, s['path'])], data_access=policy,
                processed_data=deepcopy(project.get('processed_data', [])),
                history=paths['audit'] + '/events.jsonl')


def inline(value):
    return re.sub(r'([\\`*_[\]<>|])', r'\\\1', html.escape(' '.join(str(value).split())))


def link(path, root):
    absolute = Path(path) if Path(path).is_absolute() else root / path
    href = os.path.relpath(absolute, root / 'data') if absolute.is_relative_to(root) else str(absolute)
    return '[' + inline(path) + '](' + quote(href, safe='/') + ')'


def readme(project):
    root = Path(project['path'])
    data = catalog(project)
    lines = [START, '## Data catalog', '',
             'Set approved input files and directories yourself in [access.yaml](access.yaml).',
             'Agents must use only these inputs and must not change this policy. App checks do not sandbox terminals or Python.', '',
             'Raw files stay at their original paths. Processed datasets live in `data/processed/`.',
             'Descriptions and input relationships are recorded by the author or agent. A matching notebook execution',
             'is identified when observed; this does not independently prove which process wrote a dataset.', '',
             'Machine-readable records: [lineage.json](lineage.json). Full project activity: ' + link(data['history'], root) + '.', '',
             '### Raw inputs', '']
    for source in data['raw_inputs']:
        lines += ['- **' + inline(source['id']) + '** — ' + link(source['path'], root),
                  '  ' + inline(source.get('description') or 'Description not recorded.') +
                  ' Size: ' + str(source.get('size', '?')) + ' bytes.']
    if not data['raw_inputs']:
        lines.append('No raw inputs registered.')
    lines += ['', '### Processed datasets', '']
    for item in data['processed_data']:
        producer = item['producer']
        lines += ['#### ' + link(item['path'], root), '',
                  '**What:** ' + inline(item['description']), '',
                  '**What was done:** ' + inline(item['transformation']), '',
                  '**Inputs:** ' + (', '.join(link(p['path'], root) for p in item['inputs']) or 'Synthetic data (declared).'), '',
                  '**Producing code:** ' + link(producer['path'], root) +
                  ('; cell `' + inline(producer['cell_id']) + '`' if producer.get('cell_id') else '') + '.', '',
                  '**Code fingerprint:** `' + producer['source_sha256'] + '`; ' +
                  ('matching notebook execution recorded.' if producer['execution_observed'] else 'execution was not observed by GusNotebook.'), '',
                  '**Recorded:** ' + item['recorded_at'] + ' by ' + inline(item['activity'].get('actor', 'Unspecified')) + '.', '',
                  '**Size:** ' + str(item['size']) + ' bytes.', '']
        if item['parameters']:
            lines += ['**Parameters:** ' + inline(json.dumps(item['parameters'], ensure_ascii=False)), '']
        if item['schema']:
            lines += ['**Columns:** ' + ', '.join(inline(s['name']) for s in item['schema'][:50]) +
                      (' …' if len(item['schema']) > 50 else '') + '.', '']
        if producer.get('git'):
            lines += ['**Git:** `' + producer['git']['commit'] + '`' +
                      (' with uncommitted producer changes.' if producer['git']['uncommitted'] else ' (producer clean).'), '']
    if not data['processed_data']:
        lines.append('No processed datasets recorded yet.')
    changes = [e for e in project['events'] if e['kind'] == 'processed_data_recorded']
    lines += ['', '### Dataset changes', '']
    lines += [e['time'] + ' — ' + link(e['dataset']['path'], root) + ': ' + inline(e['dataset']['transformation'])
              for e in changes[-20:]] or ['No dataset changes recorded.']
    lines += ['', 'Earlier revisions and user/agent requests remain in the full project activity log.',
              'These records describe observed or declared actions, not idle time or unobserved work in other tools.', '', END]
    return '\n'.join(lines)


def write_docs(project):
    """Update managed content, preserving handwritten README notes."""
    root = Path(project['path'])
    paths = layout_paths(project)
    # Keep the portable catalog current even if handwritten README edits need
    # attention before its managed section can be updated.
    path = root / paths['lineage']
    data = encoded(catalog(project))
    if not path.exists() or read_file(path, 32 * 1024 * 1024) != data:
        write_file(path, data)
    # Older projects acquire the processed-data folder when their catalog is created.
    (root / paths['processed']).mkdir(exist_ok=True)
    document = root / paths['data_readme']
    old = read_file(document).decode() if document.exists() else '# Project data\n\nAdd project-specific data notes outside the managed catalog below.\n\n'
    generated = readme(project)
    if START in old or END in old:
        if old.count(START) != 1 or old.count(END) != 1 or old.index(START) >= old.index(END):
            raise ProjectError('Restore the data/README.md catalog markers to refresh its generated section')
        updated = old[:old.index(START)] + generated + old[old.index(END) + len(END):]
    else:
        updated = old.rstrip() + '\n\n' + generated + '\n'
    if updated != old:
        write_file(document, updated)
