# syntax=docker/dockerfile:1.7
# dash-server as a container, for running beside a database: hosted-ready defaults, a non-root
# user, the instance directory on a mounted path, and a pip cache that already holds the wheels
# of dash-server, dash, plotly, pandas, and pyexasol, so a dashboard that uses only these
# packages builds its per-app environment without downloading them.
# Build for the target platform, for example: docker build --platform linux/arm64 -t dash-server .

ARG PYTHON_IMAGE=docker.io/library/python:3.12-slim-bookworm
# uv from PyPI, the glibc build, whose resolver works on every build network; the same version as CI.
ARG UV_VERSION=0.13.5

# The dash-server wheel and its locked runtime requirements.
FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION
RUN pip install --no-cache-dir --disable-pip-version-check "uv==${UV_VERSION}"
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist \
 && uv export --locked --no-dev --no-hashes --no-emit-project --output-file /dist/requirements.txt

FROM ${PYTHON_IMAGE}
ARG UV_VERSION
RUN pip install --no-cache-dir --disable-pip-version-check "uv==${UV_VERSION}"
# git backs the GitOps repository of every instance.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /home/dash --create-home dash \
 && mkdir -p /var/lib/dash-server /opt/dash-server/wheel-cache \
 && chown dash:dash /var/lib/dash-server /opt/dash-server/wheel-cache

COPY --from=build /dist/ /opt/dash-server/dist/
RUN uv venv --python /usr/local/bin/python3 /opt/dash-server/venv \
 && uv pip install --python /opt/dash-server/venv/bin/python --no-cache \
      --requirement /opt/dash-server/dist/requirements.txt \
 && uv pip install --python /opt/dash-server/venv/bin/python --no-cache --no-deps /opt/dash-server/dist/dash_server-*.whl
COPY container/entrypoint.sh /opt/dash-server/entrypoint.sh

ENV PATH=/opt/dash-server/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DASH_SERVER_HOST=0.0.0.0 \
    DASH_SERVER_PORT=5100 \
    DASH_SERVER_INSTANCE_PATH=/var/lib/dash-server \
    DASH_SERVER_APP_DEPENDENCY_ISOLATION=per_app \
    DASH_SERVER_APP_RUNTIME_MODE=isolated \
    DASH_SERVER_APP_WHEEL_CACHE_ROOT=/opt/dash-server/wheel-cache

USER dash
# Fill the pip cache the way a per-app environment is built: a venv of the server's Python, the
# dash-server wheel that gives workers `dash_server_runtime`, and the packages dashboards use most.
RUN python -m venv /tmp/warm \
 && /tmp/warm/bin/pip install --disable-pip-version-check --cache-dir "$DASH_SERVER_APP_WHEEL_CACHE_ROOT" \
      /opt/dash-server/dist/dash_server-*.whl dash plotly pandas pyexasol \
 && rm -rf /tmp/warm

VOLUME ["/var/lib/dash-server"]
EXPOSE 5100
ENTRYPOINT ["/opt/dash-server/entrypoint.sh"]
