"""Long-lived parent process for forkserver-style cold-start optimization.

The baseline:

1. Imports the universal subset (``dash``, ``flask``, ``dash_server_runtime``, …)
   exactly once at startup.
2. Binds a Unix domain socket and listens for fork requests.
3. For each request, ``fork()`` a child. The child closes the listening socket,
   duplicates the parent-provided stdio FDs (received via ``SCM_RIGHTS``) onto
   its own fd 1 / fd 2, and runs the same ``serve(args)`` flow the spawn path
   uses. The parent acks via the socket and goes back to listening.

Children inherit the baseline's already-imported modules. Pages that the child
doesn't write to stay shared with the baseline via copy-on-write, which is
where the memory savings come from. The cold-start time win is also large:
the parent has already paid the cost of importing Dash/Flask.

Wire protocol on the Unix socket:

- Inbound (one frame per accept): ``send_fds(json_request, [stdout_fd, stderr_fd])``
  where ``json_request`` is a single JSON document like::

      {"args": {... serve(args) Namespace fields ...}}

- Outbound: a single newline-terminated JSON line. Either::

      {"event": "forked", "pid": 12345}

  or::

      {"event": "error", "phase": "<where>", "error": "<text>"}

This keeps the baseline contract small enough to test directly with a stdlib
``socket.socket`` client. No multiprocessing magic, no pickle, no global state
beyond the modules the baseline chose to prewarm.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import traceback
from argparse import Namespace
from typing import Any

from .protocol import (
    EVENT_ERROR,
    EVENT_FAILED,
    EVENT_FORKED,
    EVENT_READY,
    KEY_EVENT,
)

# Imports the baseline pre-warms before it starts accepting fork requests.
# Children inherit this state via copy-on-write.
DEFAULT_PREWARM_PACKAGES: tuple[str, ...] = (
    "dash",
    "flask",
    "dash_server_runtime",
)


def _prewarm(packages: tuple[str, ...]) -> list[dict[str, str]]:
    """Import each package; return per-package warnings for any failures."""

    warnings: list[dict[str, str]] = []
    for name in packages:
        if not name:
            continue
        try:
            __import__(name)
        except Exception as exc:
            warnings.append({"package": name, "error": f"{type(exc).__name__}: {exc}"})
    return warnings


def _serve_in_child(request: dict[str, Any]) -> int:
    """Run inside the forked child. Mirrors the spawn-path serve() entry point."""

    from ._serve import serve  # imported lazily so the baseline doesn't pay for it

    args_dict = request.get("args") or {}
    return serve(Namespace(**args_dict))


def _handle_request(conn: socket.socket) -> None:
    """Read one fork request, fork, ack the parent."""

    # Single recv_fds — fork requests are small (<64 KiB).
    msg, fds, _, _ = socket.recv_fds(conn, 65536, 2)
    if not msg or len(fds) < 2:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            conn.sendall(
                json.dumps({KEY_EVENT: EVENT_ERROR, "phase": "recv_fds", "error": "expected two FDs"}).encode()
                + b"\n"
            )
        except OSError:
            pass
        return

    stdout_fd, stderr_fd = fds[0], fds[1]
    try:
        request = json.loads(msg)
    except json.JSONDecodeError as exc:
        for fd in (stdout_fd, stderr_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            conn.sendall(
                json.dumps(
                    {KEY_EVENT: EVENT_ERROR, "phase": "json_parse", "error": str(exc)}
                ).encode()
                + b"\n"
            )
        except OSError:
            pass
        return

    pid = os.fork()
    if pid == 0:
        # ----- Child path ---------------------------------------------------
        # Replace stdio with the parent-provided pipes so the manager sees our output.
        try:
            os.dup2(stdout_fd, 1)
            os.dup2(stderr_fd, 2)
        except OSError:
            pass
        # Close inherited socket/fds so we don't keep listening or leak refs.
        for fd in (stdout_fd, stderr_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass
        try:
            exit_code = _serve_in_child(request)
        except Exception:
            print(
                json.dumps(
                    {
                        KEY_EVENT: EVENT_FAILED,
                        "phase": "child_serve",
                        "error": traceback.format_exc(),
                    }
                ),
                flush=True,
            )
            os._exit(1)
        # _exit so we don't run atexit handlers inherited from the baseline.
        os._exit(int(exit_code or 0))

    # ----- Parent path -------------------------------------------------------
    # Close our copy of the child's stdio FDs so EOF propagates correctly when the
    # child exits.
    for fd in (stdout_fd, stderr_fd):
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        conn.sendall(
            json.dumps({KEY_EVENT: EVENT_FORKED, "pid": pid}).encode() + b"\n"
        )
    except OSError:
        pass


def _reap_dead_children(_signum: int | None = None, _frame: Any = None) -> None:
    """Reap any exited children so they don't accumulate as zombies."""

    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print(json.dumps({KEY_EVENT: EVENT_FAILED, "phase": "argparse", "error": "missing socket_path"}), flush=True)
        return 2
    socket_path = argv[0]
    if len(argv) > 1 and argv[1]:
        prewarm_packages = tuple(name.strip() for name in argv[1].split(",") if name.strip())
    else:
        prewarm_packages = DEFAULT_PREWARM_PACKAGES

    prewarm_warnings = _prewarm(prewarm_packages)

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(socket_path)
    except OSError as exc:
        print(
            json.dumps(
                {KEY_EVENT: EVENT_FAILED, "phase": "bind", "error": f"{type(exc).__name__}: {exc}", "socket_path": socket_path}
            ),
            flush=True,
        )
        return 2
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    sock.listen(8)

    # Tell the parent we're ready and what we pre-imported. The parent uses this
    # to decide which workers it can route through this baseline.
    print(
        json.dumps(
            {
                KEY_EVENT: EVENT_READY,
                "pid": os.getpid(),
                "socket_path": socket_path,
                "prewarmed_packages": list(prewarm_packages),
                "prewarm_warnings": prewarm_warnings,
                "python_executable": sys.executable,
            }
        ),
        flush=True,
    )

    # Signal handlers for graceful shutdown + zombie reaping.
    def _terminate(_signum, _frame):
        try:
            sock.close()
        finally:
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass
            sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _terminate)
        signal.signal(signal.SIGINT, _terminate)
        signal.signal(signal.SIGCHLD, _reap_dead_children)
    except (ValueError, AttributeError):
        pass

    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            break
        try:
            _handle_request(conn)
        except Exception:
            try:
                conn.sendall(
                    json.dumps(
                        {
                            KEY_EVENT: EVENT_ERROR,
                            "phase": "request",
                            "error": traceback.format_exc(),
                        }
                    ).encode()
                    + b"\n"
                )
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
