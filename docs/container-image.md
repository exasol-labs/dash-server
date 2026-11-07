# Container Image

The repository's `Dockerfile` builds `dash-server` as a container image for running it as a hosted service, for example beside the Exasol database its dashboards query.

```bash
docker build --platform linux/arm64 -t dash-server .
```

Build for the platform the container runs on; the image works on `linux/arm64` and `linux/amd64`.

## What The Image Contains

- Python and `uv`, with `dash-server` installed from its locked requirements.
- `git`, which the GitOps repository of every instance needs.
- A non-root user, `dash` (UID 10001), that the server runs as.
- The instance directory at `/var/lib/dash-server`, declared as a volume; mount persistent storage there to keep dashboards, revisions, the registry, and secrets across restarts.
- A pip cache at `/opt/dash-server/wheel-cache` that already holds the wheels of `dash-server`, `dash`, `plotly`, `pandas`, and `pyexasol`. Per-app environments install from it, so a dashboard that needs only these packages builds its environment without downloading them.

## Defaults

The image sets the runtime isolation that hosted mode requires and listens on all interfaces:

| Variable | Value |
| --- | --- |
| `DASH_SERVER_HOST` | `0.0.0.0` |
| `DASH_SERVER_PORT` | `5100` |
| `DASH_SERVER_INSTANCE_PATH` | `/var/lib/dash-server` |
| `DASH_SERVER_APP_DEPENDENCY_ISOLATION` | `per_app` |
| `DASH_SERVER_APP_RUNTIME_MODE` | `isolated` |
| `DASH_SERVER_APP_WHEEL_CACHE_ROOT` | `/opt/dash-server/wheel-cache` |
| `DASH_SERVER_APP_HELPER_PACKAGE` | the image's `dash-server` wheel, which gives workers `dash_server_runtime` |

Everything else comes from the environment as described in the [Hosted Mode Admin Guide](hosted-mode.md) and [Exasol](exasol.md). A hosted container behind an authenticating proxy in the same pod or host could add:

```bash
DASH_SERVER_MODE=hosted
DASH_SERVER_HOST=127.0.0.1
DASH_SERVER_SECRET_KEY=...
DASH_SERVER_PUBLIC_BASE_URL=https://dashboards.example.com
DASH_SERVER_SESSION_COOKIE_SECURE=true
DASH_SERVER_AUTH_PROVIDER=trusted_proxy
DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED=true
DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS=127.0.0.1/32
DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS=trusted_proxy:owner
DASH_SERVER_EXASOL_PROFILE_NAME=default
DASH_SERVER_EXASOL_DSN=localhost:8563
DASH_SERVER_EXASOL_USER=sys
EXA_PASSWORD=...
DASH_SERVER_EXASOL_PROFILE_CREATION_ENABLED=false
DASH_SERVER_DEMO_APP_ENABLED=false
```

## Testing The Image

`tests/test_container_image.py` starts a built image in hosted mode and checks that it runs as the non-root user, serves under a configured path prefix, and builds a dashboard's environment from the cached wheels. It runs when `DASH_SERVER_TEST_IMAGE` names the image, with `docker` or the CLI in `DASH_SERVER_CONTAINER_CLI`:

```bash
DASH_SERVER_TEST_IMAGE=dash-server uv run pytest tests/test_container_image.py
```
