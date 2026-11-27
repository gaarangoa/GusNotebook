"""Browse and manage native histories without copying transcripts into app state.

Adapters expose the same small catalog and text preview. Native CLIs remain
responsible for restoring conversations. Mutations target the selected history.
"""

from collections import deque
from contextlib import closing
from datetime import datetime
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
from uuid import UUID

from .persistence import atomic_write
from .agent_session_actions import codex_request

WINDOW = 512 * 1024
MAX_FILES = 10000
PAGE_SIZE = 100


def valid_id(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def timestamp(value):
    try:
        return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def records(path, tail=False):
    """Bound reads even for transcripts containing enormous tool/image results."""
    with path.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        start = max(0, size - WINDOW) if tail else 0
        stream.seek(start)
        data = stream.read(WINDOW)
    lines = data.splitlines()
    if start:
        lines = lines[1:]  # May start inside a JSON string or a UTF-8 character.
    for line in lines:
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                yield record
        except (ValueError, UnicodeError, RecursionError):
            continue


def message(record, provider):
    if provider == 'codex':
        if record.get('type') != 'response_item':
            return None
        item = record.get('payload') or {}
        if not isinstance(item, dict) or item.get('type') != 'message' or item.get('channel') == 'analysis':
            return None
    else:
        if record.get('type') not in {'user', 'assistant'} or record.get('isSidechain'):
            return None
        item = record.get('message') or {}
    if not isinstance(item, dict) or item.get('role') not in {'user', 'assistant'}:
        return None
    content = item.get('content', '')
    if isinstance(content, list):
        content = '\n'.join(block.get('text', '') for block in content if isinstance(block, dict)
                            and block.get('type') in {'text', 'input_text', 'output_text'}
                            and isinstance(block.get('text'), str))
    if not isinstance(content, str) or not content.strip():
        return None
    if item['role'] == 'user' and content.lstrip().startswith((
            '# AGENTS.md', '<environment_context>', '<permissions instructions>',
            '<system-reminder>', '<local-command', '<command-name>')):
        return None
    return {'role': item['role'], 'text': content[:12000], 'at': record.get('timestamp'),
            'truncated': len(content) > 12000}


@lru_cache(maxsize=2048)
def file_metadata(provider, filename, modified, size):
    path = Path(filename)
    row = {'id': None, 'cwd': None, 'title': '', 'created': 0,
           'updated': modified, 'provider': provider, '_path': filename}
    head = list(records(path))
    for record in head:
        if provider == 'codex' and record.get('type') == 'session_meta':
            meta = record.get('payload') or {}
            if not isinstance(meta, dict) or isinstance(meta.get('source'), dict) or str(meta.get('source', '')).startswith('subagent'):
                return None
            row.update(id=meta.get('id') or meta.get('session_id'), cwd=meta.get('cwd'),
                       created=timestamp(meta.get('timestamp') or record.get('timestamp')))
        elif provider == 'claude' and record.get('cwd'):
            if record.get('isSidechain'):
                return None
            row['id'] = row['id'] or record.get('sessionId')
            row['cwd'] = row['cwd'] or record['cwd']
            row['created'] = row['created'] or timestamp(record.get('timestamp'))
        text = message(record, provider)
        if not row['title'] and text and text['role'] == 'user':
            row['title'] = ' '.join(text['text'].split())[:180]
    if provider == 'claude':
        custom_title = None
        for record in head + (list(records(path, tail=True)) if size > WINDOW else []):
            if record.get('type') in {'custom-title', 'summary'}:
                title = record.get('customTitle') or record.get('summary')
                if isinstance(title, str) and title.strip():
                    row['title'] = title[:180]
                    if record.get('type') == 'custom-title':
                        custom_title = title[:180]
        if custom_title:
            row['title'] = custom_title
    if not valid_id(row['id']) or not isinstance(row['cwd'], str) or not Path(row['cwd']).is_absolute():
        return None
    row['cwd'] = str(Path(row['cwd']).resolve())
    row['title'] = row['title'] or f'{provider.title()} conversation'
    return row


class CodexHistory:
    name = 'codex'
    label = 'Codex'

    def __init__(self, root):
        self.root = root

    def rename(self, row, title):
        self.checked_path(row)
        codex_request(self.root, 'thread/name/set', {'threadId': row['id'], 'name': title})

    def delete(self, row):
        self.checked_path(row)
        codex_request(self.root, 'thread/delete', {'threadId': row['id']})

    def checked_path(self, row):
        path = Path(row['_path'])
        if not path.resolve().is_relative_to((self.root / 'sessions').resolve()) or not path.is_file():
            raise ValueError('Saved session is outside the native Codex history directory.')
        return path

    def rows(self):
        rows = {}
        excluded = set()
        # Recent Codex versions maintain this index. Read known columns only and
        # fall back to rollouts for older versions or a busy/incompatible index.
        for db in sorted(self.root.glob('state_*.sqlite'), reverse=True)[:1]:
            try:
                with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=.2)) as conn:
                    columns = {r[1] for r in conn.execute('pragma table_info(threads)')}
                    wanted = {'id', 'cwd', 'title', 'created_at', 'updated_at', 'rollout_path', 'source', 'archived', 'name'} & columns
                    if not {'id', 'cwd', 'rollout_path', 'updated_at'} <= wanted:
                        continue
                    conn.row_factory = sqlite3.Row
                    for result in conn.execute('select ' + ','.join(sorted(wanted)) + ' from threads order by updated_at desc limit ?', (MAX_FILES,)):
                        item = dict(result)
                        path = Path(item['rollout_path']).resolve()
                        if item.get('archived') or 'subagent' in str(item.get('source', '')):
                            excluded.add(item['id'])
                            continue
                        if not path.is_relative_to((self.root / 'sessions').resolve()) or not path.is_file():
                            continue
                        if not valid_id(item['id']) or not isinstance(item.get('cwd'), str) or not Path(item['cwd']).is_absolute():
                            continue
                        rows[str(path)] = dict(id=item['id'], cwd=str(Path(item['cwd']).resolve()),
                            title=(item.get('name') or item.get('title') or 'Codex conversation')[:180],
                            created=item.get('created_at', 0), updated=item['updated_at'],
                            provider=self.name, _path=str(path))
            except (sqlite3.Error, OSError, ValueError, TypeError):
                continue
        names = {}
        try:
            for item in records(self.root / 'session_index.jsonl', tail=True):
                if isinstance(item.get('thread_name'), str):
                    names[item.get('id')] = item['thread_name'][:180]
        except OSError:
            pass
        count = 0
        for path in (self.root / 'sessions').glob('*/*/*/*.jsonl'):
            count += 1
            if count > MAX_FILES:
                break
            try:
                key = str(path.resolve())
                if key not in rows:
                    stat = path.stat()
                    row = file_metadata(self.name, key, stat.st_mtime, stat.st_size)
                    if row:
                        rows[key] = row.copy()
            except (OSError, ValueError, TypeError):
                continue
        for row in rows.values():
            if row['id'] in names:
                row['title'] = names[row['id']]
        return [row for row in rows.values() if row['id'] not in excluded], count > MAX_FILES


class ClaudeHistory:
    name = 'claude'
    label = 'Claude'

    def __init__(self, root):
        self.root = root

    def checked_path(self, row):
        path = Path(row['_path'])
        if (path.resolve().parent.parent != (self.root / 'projects').resolve() or
                path.name != row['id'] + '.jsonl' or path.is_symlink() or not path.is_file()):
            raise ValueError('Saved session is outside the native Claude history directory.')
        return path

    def rename(self, row, title):
        path = self.checked_path(row)
        # Same append-only custom-title record as Claude's native session API.
        # Do not create a transcript if another process has already deleted it.
        data = ('\n' + json.dumps({'type': 'custom-title', 'customTitle': title,
                                   'sessionId': row['id']}) + '\n').encode()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            if os.write(fd, data) != len(data):
                raise OSError('Incomplete session title write; refresh and retry.')
            os.fsync(fd)
        finally:
            os.close(fd)

    def delete(self, row):
        path = self.checked_path(row)
        children = path.parent / row['id']
        # Refuse links rather than following them out of the session directory.
        if children.is_symlink():
            raise ValueError('Cannot delete a session whose subagent directory is a symbolic link.')
        if children.exists():
            shutil.rmtree(children)
        path.unlink()

    def rows(self):
        rows = []
        count = 0
        # Only top-level conversations, not projects/*/<id>/subagents/*.jsonl.
        for path in (self.root / 'projects').glob('*/*.jsonl'):
            count += 1
            if count > MAX_FILES:
                break
            try:
                stat = path.stat()
                row = file_metadata(self.name, str(path.resolve()), stat.st_mtime, stat.st_size)
                if row:
                    rows.append(row.copy())
            except (OSError, ValueError, TypeError):
                continue
        return rows, count > MAX_FILES


class AgentSessions:
    def __init__(self, state, providers=None):
        self.state = Path(state)
        self.providers = providers if providers is not None else [
            CodexHistory(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()),
            ClaudeHistory(Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser().resolve()),
        ]
        self.lock = threading.RLock()
        try:
            self.bindings = json.loads(self.state.read_text())
            if not isinstance(self.bindings, dict):
                self.bindings = {}
        except (OSError, ValueError):
            self.bindings = {}

    def remember(self, provider, session_id, project):
        if not valid_id(session_id) or provider not in {p.name for p in self.providers}:
            return
        with self.lock:
            key = provider + ':' + session_id
            if key in self.bindings and self.bindings[key] == project:
                return
            self.bindings[key] = project
            atomic_write(self.state, json.dumps(self.bindings), mode=0o600)

    def catalog(self, folder, children=False):
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError('Choose an existing folder in Files.')
        rows, truncated = {}, False
        providers = []
        for provider in self.providers:
            available = bool(shutil.which(provider.name))
            providers.append(dict(id=provider.name, name=provider.label, available=available))
            items, limited = provider.rows()
            truncated |= limited
            for row in items:
                cwd = Path(row['cwd'])
                if cwd != folder and not (children and cwd.is_relative_to(folder)):
                    continue
                key = provider.name + ':' + row['id']
                rows[key] = {**row, 'available': available, 'manageable': available or provider.name == 'claude',
                             'project': self.bindings.get(key)}
        return dict(folder=str(folder), sessions=sorted(rows.values(), key=lambda r: r['updated'], reverse=True),
                    providers=providers, truncated=truncated)

    def find(self, folder, provider, session_id):
        if not folder:
            raise ValueError('Choose a folder in Files first.')
        if not valid_id(session_id):
            raise ValueError('Invalid saved session ID.')
        row = next((r for r in self.catalog(folder)['sessions'] if r['provider'] == provider and r['id'] == session_id), None)
        if row is None:
            raise ValueError('Saved session was not found in this folder. Refresh the list.')
        return row

    def preview(self, row):
        path = Path(row['_path'])
        messages = deque(maxlen=100)
        total = 0
        truncated = path.stat().st_size > WINDOW
        for record in records(path, tail=True):
            item = message(record, row['provider'])
            if item:
                total += 1
                messages.append(item)
        # Limit the HTTP response as well as the disk read.
        kept, size = [], 0
        for item in reversed(messages):
            if size + len(item['text']) > 80000:
                break
            kept.append(item)
            size += len(item['text'])
        return dict(messages=list(reversed(kept)), truncated=truncated or total > len(kept) or any(m['truncated'] for m in kept))

    def rename(self, row, title):
        if (not isinstance(title, str) or not title.strip() or len(title.strip()) > 180 or
                any(ord(char) < 32 or ord(char) == 127 for char in title)):
            raise ValueError('Enter a title of 1–180 characters without line breaks or control characters.')
        title = title.strip()
        provider = next(p for p in self.providers if p.name == row['provider'])
        provider.rename(row, title)
        file_metadata.cache_clear()
        return self.find(row['cwd'], row['provider'], row['id'])

    def delete(self, row):
        provider = next(p for p in self.providers if p.name == row['provider'])
        provider.delete(row)
        file_metadata.cache_clear()
        self.bindings.pop(row['provider'] + ':' + row['id'], None)
        try:
            atomic_write(self.state, json.dumps(self.bindings), mode=0o600)
        except OSError:
            # Native deletion already succeeded; a stale association alone must
            # not report a failed delete that can no longer be retried.
            logging.getLogger(__name__).warning('Deleted agent conversation; could not clear its saved project association')


def public(row):
    return {key: value for key, value in row.items() if not key.startswith('_')}
