"""Wire-protocol constants for the out-of-process worker.

The worker and the control-plane ``AppWorkerManager`` talk to each other with
newline-delimited JSON event lines across a process boundary. The event names and
payload keys used to be raw string literals scattered across the emitting side
(``_serve.py`` / ``baseline.py``) and the parsing side
(``dash_server.runtime.worker_manager``). A typo or rename on one side would not fail
compilation or tests — it would only surface at runtime as a "worker did not emit a
ready event" timeout.

Centralizing the strings here makes a rename break both sides at import time instead.

This module has **no** ``dash_server`` dependency and imports nothing beyond the stdlib,
so it is safe to import inside a per-app environment that only ships
``dash_server_runtime``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Payload keys
# ---------------------------------------------------------------------------

#: Discriminator key present on every structured worker event line.
KEY_EVENT = "event"
#: Bound TCP port reported on a ``ready`` event from a serve worker.
KEY_PORT = "port"
#: Loopback host reported on a ``ready`` event.
KEY_HOST = "host"
#: OS process id reported on ``ready`` / ``forked`` events.
KEY_PID = "pid"
#: Sub-stage label carried by ``failed`` / ``warning`` / ``error`` events.
KEY_PHASE = "phase"
#: HTTP status code carried by a ``response`` event.
KEY_STATUS = "status"

# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------

#: A serve worker (or a forkserver baseline) has finished starting up.
EVENT_READY = "ready"
#: A worker failed during a startup/serve stage; carries ``KEY_PHASE`` + ``error``.
EVENT_FAILED = "failed"
#: A forkserver baseline successfully forked a child; carries ``KEY_PID``.
EVENT_FORKED = "forked"
#: A forwarded HTTP response was observed; carries ``KEY_STATUS``.
EVENT_RESPONSE = "response"
#: A non-fatal degradation (e.g. optional bootstrap failed); carries ``KEY_PHASE``.
EVENT_WARNING = "warning"
#: A baseline-level protocol/request error; carries ``KEY_PHASE`` + ``error``.
EVENT_ERROR = "error"

#: Events that terminate a "read until ready" loop: either the worker is up
#: (``ready``) or it gave up (``failed``). Both the spawn and forkserver read paths
#: in ``worker_manager`` stop on these.
READY_READ_EVENTS = frozenset({EVENT_READY, EVENT_FAILED})

__all__ = [
    "EVENT_ERROR",
    "EVENT_FAILED",
    "EVENT_FORKED",
    "EVENT_READY",
    "EVENT_RESPONSE",
    "EVENT_WARNING",
    "KEY_EVENT",
    "KEY_HOST",
    "KEY_PHASE",
    "KEY_PID",
    "KEY_PORT",
    "KEY_STATUS",
    "READY_READ_EVENTS",
]
