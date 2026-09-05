"""Tiny pub/sub so kernel execution can push events to SSE listeners."""

import json
import queue
import threading

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


class Bus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event_type, **payload):
        event = dict(payload, type=event_type)
        who = origin()
        if who and "origin" not in event:
            event["origin"] = who
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Resynchronize instead of silently losing document changes.
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait({"type": "resync"})
                except queue.Full:
                    pass


_fallback = Bus()


def current():
    from flask import current_app, has_app_context
    if has_app_context() and "gusnotebook" in current_app.extensions:
        return current_app.extensions["gusnotebook"].bus
    return _fallback


def publisher():
    return current().publish


def publish(event_type, **payload):
    current().publish(event_type, **payload)


def subscribe():
    return current().subscribe()


def unsubscribe(q):
    current().unsubscribe(q)


def format_sse(event):
    return "data: " + json.dumps(event) + "\n\n"
