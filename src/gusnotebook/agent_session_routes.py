"""Folder-scoped discovery and previews for saved agent conversations."""

from flask import Blueprint, current_app, jsonify, request

from .agent_sessions import PAGE_SIZE, public
from . import bus

agent_session_routes = Blueprint('agent_sessions', __name__)


def live_terminals(row):
    return [term for term in current_app.extensions['gusnotebook'].terms.list()
            if term['alive'] and term['kind'] == row['provider'] and term.get('agent_session') == row['id']]


def describe(row):
    return {**public(row), 'running': bool(live_terminals(row))}


@agent_session_routes.get('/api/agent-sessions')
def catalog():
    from .app import request_session
    manager = current_app.extensions['gusnotebook'].agent_sessions
    try:
        data = manager.catalog(request.args.get('folder') or request_session().root,
                               request.args.get('children') == '1')
        provider = request.args.get('provider', '')
        query = request.args.get('q', '').casefold()[:500]
        rows = [row for row in data.pop('sessions')
                if (not provider or row['provider'] == provider) and
                (not query or query in (row['title'] + ' ' + row['id'] + ' ' + row['cwd']).casefold())]
        offset = max(0, int(request.args.get('offset', 0)))
        return jsonify(**data, sessions=[describe(row) for row in rows[offset:offset + PAGE_SIZE]],
                       total=len(rows), next_offset=offset + PAGE_SIZE if offset + PAGE_SIZE < len(rows) else None)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@agent_session_routes.get('/api/agent-sessions/preview')
def preview():
    manager = current_app.extensions['gusnotebook'].agent_sessions
    try:
        row = manager.find(request.args.get('folder', ''), request.args.get('provider'), request.args.get('id'))
        return jsonify(session=describe(row), **manager.preview(row))
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@agent_session_routes.route('/api/agent-sessions', methods=['PATCH', 'DELETE'])
def mutate():
    runtime = current_app.extensions['gusnotebook']
    manager = runtime.agent_sessions
    if request.content_length and request.content_length > 12000:
        return jsonify(error='Session action request is too large.'), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('folder'), str):
        return jsonify(error='Choose a saved agent session and its folder.'), 400
    try:
        with manager.lock:
            row = manager.find(data['folder'], data.get('provider'), data.get('id'))
            if request.method == 'DELETE':
                if live_terminals(row):
                    return jsonify(error='This conversation is running. Close its agent terminal before deleting it.'), 409
                manager.delete(row)
                bus.publish('agent_session_changed', provider=row['provider'], id=row['id'], deleted=True)
                return jsonify(deleted=True, id=row['id'])
            row = manager.rename(row, data.get('title'))
            changed = []
            for info in runtime.terms.list():
                if info['kind'] == row['provider'] and info.get('agent_session') == row['id']:
                    terminal = runtime.terms.get(info['id'])
                    if terminal:
                        terminal.label = row['title'][:60]
                        changed.append(terminal.id)
            event = dict(provider=row['provider'], id=row['id'], title=row['title'], terminals=changed)
            bus.publish('agent_session_changed', **event)
            return jsonify(session=describe(row), **event)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400
