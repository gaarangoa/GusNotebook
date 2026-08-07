"""Tiny pub/sub so kernel execution can push events to SSE listeners."""

import json
import queue
import threading

_lock = threading.Lock()
_subscribers = set()

# Who caused the events published on this thread, if anyone identified
# themselves. Set per request (see app.set_origin) and stamped onto every event
# published while handling it, so a browser can recognise the echo of its own
# edit and skip the reload it already did locally. Thread-local because Flask
# runs each request on its own thread; events from a kernel or a watcher thread
# have no origin, which is correct — nobody in a browser caused them.
_local = threading.local()


def set_origin(client_id):
    """Tag events published on this thread as coming from `client_id`."""
    _local.origin = client_id or None


def origin():
    return getattr(_local, "origin", None)


def subscribe():
    """Register a listener. Returns a queue that receives event dicts."""
    q = queue.Queue(maxsize=1000)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q):
    with _lock:
        _subscribers.discard(q)


def publish(event_type, **payload):
    """Broadcast an event to every listener. Never blocks on a slow client."""
    event = dict(payload)
    event["type"] = event_type
    who = origin()
    if who and "origin" not in event:
        event["origin"] = who
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def format_sse(event):
    return "data: " + json.dumps(event) + "\n\n"
