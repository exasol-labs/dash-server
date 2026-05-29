# Runtime Modes

`dash-server` has two orthogonal isolation axes, each with two settings. The four combinations are
all valid; this page covers when to pick which.

## The two flags

```bash
DASH_SERVER_APP_DEPENDENCY_ISOLATION = shared | per_app     # default: shared
DASH_SERVER_APP_RUNTIME_MODE         = in_process | isolated # default: in_process
```

| Flag                          | Effect                                                                                                                                                                                |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `APP_DEPENDENCY_ISOLATION`    | Where dashboard `requirements.txt` is installed. `shared` → into the server's interpreter. `per_app` → into a per-(dependency_lock_hash) venv under `instance/app_envs/`.              |
| `APP_RUNTIME_MODE`            | Where dashboard callbacks execute. `in_process` → in the control-plane Flask instance. `isolated` → in a subprocess that serves on a loopback port behind a WSGI proxy.                |

## The matrix

|                       | `in_process`                                                            | `isolated`                                                                                                  |
| --------------------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **`shared` deps**     | **Default for local dev.** Fast startup, one Python env, no GC.        | Useful for early bring-up. Worker uses the server's own python; you get process isolation without env work. |
| **`per_app` deps**    | Validation goes to subprocess; serving stays in-process.                | **Default for hosted mode.** Workers run from per-app envs. Required when `DASH_SERVER_MODE=hosted`.        |

Hosted mode refuses anything but `per_app` + `isolated` unless `DASH_SERVER_ALLOW_UNSAFE_INPROCESS=
true` is set (development override, emits a warning on startup).

## Operator-visible artifacts

Each isolation feature has its own directory under `instance/`. None of them store secrets.

| Path                     | What it holds                                                                                                                                                                  |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `instance/app_envs/`     | One venv per `dependency_lock_hash`. Identical-deps apps share one venv. Configure root with `DASH_SERVER_APP_ENVIRONMENTS_ROOT`. |
| `instance/wheels/`       | Content-addressed wheel cache (`UV_CACHE_DIR`). Envs hardlink from here so disk cost is sublinear in app count. Configure with `DASH_SERVER_APP_WHEEL_CACHE_ROOT`.              |
| `instance/pycache/`      | Shared bytecode cache (`PYTHONPYCACHEPREFIX`). Best-effort, safe to delete. Configure with `DASH_SERVER_APP_PYCACHE_ROOT`.                                                     |
| `instance/workers/`      | One JSON per running worker. Survives control-plane restart — `AppRuntimeService.bootstrap()` adopts live workers and reaps dead ones from these records.                      |
| `instance/diagnostics/`  | Per-app JSONL log channels including `worker.jsonl` (stdout from isolated workers) and `worker.events.jsonl` (structured events like `forkserver_miss`, `restart_capped`).      |

## Key config knobs

```bash
# Disk caps (in GB)
DASH_SERVER_APP_ENVIRONMENTS_DISK_CAP_GB        # default 5.0
DASH_SERVER_APP_WHEEL_CACHE_DISK_CAP_GB         # default 2.0
DASH_SERVER_APP_ENV_GC_RETENTION_DAYS           # default 7

# Worker lifecycle
DASH_SERVER_APP_WORKER_START_TIMEOUT_SECONDS    # default 30
DASH_SERVER_APP_WORKER_IDLE_STOP_SECONDS        # default 600; 0 disables idle-stop
DASH_SERVER_APP_WORKER_MAX_RESTARTS_PER_5_MINUTES  # default 5
DASH_SERVER_APP_WORKER_HOST                     # default 127.0.0.1
DASH_SERVER_APP_WORKER_PORT_RANGE               # optional START-END; default unset uses OS ephemeral ports

# Forkserver baseline (memory-sharing optimization)
DASH_SERVER_APP_WORKER_PREWARM_POOL_SIZE        # default 1; 0 disables forkserver (spawn-only)
DASH_SERVER_APP_WORKER_PREWARM_PACKAGES         # comma-list; default "dash,plotly,pyexasol,dash_server_runtime"
```

The main `dash-server` control-plane listener is configured separately with
`DASH_SERVER_HOST`, `DASH_SERVER_PORT`, or the CLI flags `--host` and `--port`.
It defaults to `127.0.0.1:5100`.

On macOS Monterey 12 and later, AirPlay Receiver can bind ports `5000` and
`7000`. `dash-server` defaults to `5100` instead of Flask's classic `5000` to
avoid that local macOS collision. Keep `5000` and `7000` out of
`DASH_SERVER_APP_WORKER_PORT_RANGE` if you set a worker range on Macs where
AirPlay Receiver is enabled.

## How to choose

- **Local development, one or two apps:** `shared` + `in_process` (the default). Fast iteration; nothing
  to GC; no subprocess overhead.
- **Local development, mixed-dependency apps:** `per_app` + `in_process`. Stops a `pip install
  pandas==2.1` in app B from clobbering `pandas==2.0` in app A. Still runs callbacks in-process so a
  crash kills the control plane — fine for local dev, not for production.
- **Hosted single-tenant or pre-production:** `shared` + `isolated`. Process isolation without the
  disk cost of per-app envs. Restart-on-idle keeps RAM bounded.
- **Hosted multi-tenant (required setting):** `per_app` + `isolated`. Each app has its own python and
  its own process. Forkserver baseline shares pre-imported Dash/Flask pages via copy-on-write so
  cold-start stays under 500 ms.

## Observability

The "four numbers" land on two MCP resources:

```text
dash://runtime/workers
    workers: [ {mount_path, pid, port, rss_bytes, last_response_status, last_request_at, status, ...} ]
    baselines: [ {python_executable, pid, socket_path, prewarmed_packages, alive} ]
    rss_bytes_total: <int>
    last_start_ms_p50: <float | null>
    worker_count, idle_count

dash://runtime/environments
    environments: [ {environment_id, python_executable, bytes_on_disk, last_used_at, status, ...} ]
    bytes_on_disk_total
    wheel_cache_bytes
    environment_count
```

The `app_run_healthcheck` probe set gains two entries in `isolated` mode:

- `worker_alive` — manager-side liveness (pid + RSS)
- `worker_http` — proxy's last forwarded HTTP status (no extra roundtrip)

In `in_process` mode both probes return `status: "not_applicable"`.

## Audit trail

Every operational decision the runtime makes — `forkserver_miss`, `worker_adopted`, `worker_reaped`,
`restart_capped`, `env_evicted`, `wheel_cache_pruned` — is appended to the per-app `worker.events`
channel as a structured JSON line. Read via:

```text
dash://apps/<name>/logs/worker.events
```

## What this is *not*

Process isolation is **not** a security sandbox. The worker process inherits the parent's filesystem
and network access. A hostile dashboard can still:

- read files the server user can read,
- open outbound network connections,
- exhaust memory or CPU,
- enumerate other workers via the loopback interface.

Real sandboxing (containers, seccomp, restricted filesystems, network policy) is a separate effort
tracked in `plans/runtime-sandboxing-adapter-plan.md` (forthcoming). Treat the isolated runtime as
operational isolation: workers can't take down each other or the control plane, dependencies can't
silently collide, and idle workers don't accumulate RAM. That's the whole bargain.
