"""Folder-scoped discovery and previews for saved agent conversations."""

from flask import Blueprint, current_app, jsonify, request

from .agent_sessions import PAGE_SIZE, public

agent_session_routes = Blueprint('agent_sessions', __name__)


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
        return jsonify(**data, sessions=[public(row) for row in rows[offset:offset + PAGE_SIZE]],
                       total=len(rows), next_offset=offset + PAGE_SIZE if offset + PAGE_SIZE < len(rows) else None)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@agent_session_routes.get('/api/agent-sessions/preview')
def preview():
    manager = current_app.extensions['gusnotebook'].agent_sessions
    try:
        row = manager.find(request.args.get('folder', ''), request.args.get('provider'), request.args.get('id'))
        return jsonify(session=public(row), **manager.preview(row))
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400
