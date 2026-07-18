"""Configuration objects for dash-server."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class Config:
    """Default configuration for local development."""

    TESTING = False
    DASH_SERVER_HOST: str = os.environ.get("DASH_SERVER_HOST", "127.0.0.1")
    DASH_SERVER_PORT: int = int(os.environ.get("DASH_SERVER_PORT", "5100"))
    SECRET_KEY: str | None = os.environ.get("DASH_SERVER_SECRET_KEY")
    SESSION_COOKIE_SECURE: bool = _env_bool("DASH_SERVER_SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_HTTPONLY: bool = _env_bool("DASH_SERVER_SESSION_COOKIE_HTTPONLY", True)
    SESSION_COOKIE_SAMESITE: str | None = os.environ.get("DASH_SERVER_SESSION_COOKIE_SAMESITE", "Lax")
    DASH_SERVER_MODE: str = os.environ.get("DASH_SERVER_MODE", "local")
    DASH_SERVER_AUTH_ENABLED: bool | None = (
        _env_bool("DASH_SERVER_AUTH_ENABLED", False)
        if os.environ.get("DASH_SERVER_AUTH_ENABLED") is not None
        else None
    )
    DASH_SERVER_AUTH_PROVIDER: str | None = os.environ.get("DASH_SERVER_AUTH_PROVIDER")
    DASH_SERVER_PUBLIC_BASE_URL: str | None = os.environ.get("DASH_SERVER_PUBLIC_BASE_URL")
    DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED: bool = _env_bool(
        "DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED",
        False,
    )
    DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS: tuple[str, ...] = tuple(
        item.strip()
        for item in os.environ.get("DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS", "").split(",")
        if item.strip()
    )
    DASH_SERVER_OIDC_ISSUER: str | None = os.environ.get("DASH_SERVER_OIDC_ISSUER")
    DASH_SERVER_OIDC_CLIENT_ID: str | None = os.environ.get("DASH_SERVER_OIDC_CLIENT_ID")
    DASH_SERVER_OIDC_CLIENT_SECRET_REF: str | None = os.environ.get("DASH_SERVER_OIDC_CLIENT_SECRET_REF")
    DASH_SERVER_OIDC_REDIRECT_URI: str | None = os.environ.get("DASH_SERVER_OIDC_REDIRECT_URI")
    DASH_SERVER_OIDC_AUTHORIZATION_ENDPOINT: str | None = os.environ.get(
        "DASH_SERVER_OIDC_AUTHORIZATION_ENDPOINT"
    )
    DASH_SERVER_OIDC_SCOPES: str = os.environ.get("DASH_SERVER_OIDC_SCOPES", "openid email profile")
    DASH_SERVER_OIDC_GROUPS_CLAIM: str = os.environ.get("DASH_SERVER_OIDC_GROUPS_CLAIM", "groups")
    DASH_SERVER_OIDC_ORG_CLAIM: str | None = os.environ.get("DASH_SERVER_OIDC_ORG_CLAIM")
    DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS: bool = _env_bool(
        "DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS",
        False,
    )
    DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED: bool = _env_bool(
        "DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED",
        False,
    )
    DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS: tuple[str, ...] = tuple(
        item.strip()
        for item in os.environ.get("DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS", "").split(",")
        if item.strip()
    )
    DASH_SERVER_TRUSTED_PROXY_USER_HEADER: str = os.environ.get(
        "DASH_SERVER_TRUSTED_PROXY_USER_HEADER",
        "X-Forwarded-User",
    )
    DASH_SERVER_TRUSTED_PROXY_EMAIL_HEADER: str = os.environ.get(
        "DASH_SERVER_TRUSTED_PROXY_EMAIL_HEADER",
        "X-Forwarded-Email",
    )
    DASH_SERVER_TRUSTED_PROXY_GROUPS_HEADER: str = os.environ.get(
        "DASH_SERVER_TRUSTED_PROXY_GROUPS_HEADER",
        "X-Forwarded-Groups",
    )
    DASH_SERVER_EMAIL_PROVIDER: str = os.environ.get("DASH_SERVER_EMAIL_PROVIDER", "manual")
    DASH_SERVER_EMAIL_FROM: str | None = os.environ.get("DASH_SERVER_EMAIL_FROM")
    DASH_SERVER_EMAIL_FROM_NAME: str = os.environ.get("DASH_SERVER_EMAIL_FROM_NAME", "Dash Server")
    DASH_SERVER_EMAIL_REPLY_TO: str | None = os.environ.get("DASH_SERVER_EMAIL_REPLY_TO")
    DASH_SERVER_EMAIL_SMTP_HOST: str | None = os.environ.get("DASH_SERVER_EMAIL_SMTP_HOST")
    DASH_SERVER_EMAIL_SMTP_PORT: int = int(os.environ.get("DASH_SERVER_EMAIL_SMTP_PORT", "587"))
    DASH_SERVER_EMAIL_SMTP_USERNAME: str | None = os.environ.get("DASH_SERVER_EMAIL_SMTP_USERNAME")
    DASH_SERVER_EMAIL_SMTP_PASSWORD: str | None = os.environ.get("DASH_SERVER_EMAIL_SMTP_PASSWORD")
    DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR: str | None = os.environ.get(
        "DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR"
    )
    DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH: bool = _env_bool(
        "DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH",
        False,
    )
    DASH_SERVER_EMAIL_SMTP_USE_TLS: bool | None = (
        _env_bool("DASH_SERVER_EMAIL_SMTP_USE_TLS", True)
        if os.environ.get("DASH_SERVER_EMAIL_SMTP_USE_TLS") is not None
        else None
    )
    DASH_SERVER_EMAIL_SMTP_USE_SSL: bool = _env_bool("DASH_SERVER_EMAIL_SMTP_USE_SSL", False)
    DASH_SERVER_EMAIL_SMTP_TIMEOUT_SECONDS: int = int(
        os.environ.get("DASH_SERVER_EMAIL_SMTP_TIMEOUT_SECONDS", "15")
    )
    DASH_SERVER_EMAIL_SES_REGION: str | None = os.environ.get("DASH_SERVER_EMAIL_SES_REGION")
    DASH_SERVER_CONSUMPTION_ENABLED: bool = _env_bool(
        "DASH_SERVER_CONSUMPTION_ENABLED",
        True,
    )
    DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS: tuple[str, ...] = tuple(
        item.strip().lower()
        for item in os.environ.get(
            "DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS",
            "csv,xlsx,pdf,png,pptx",
        ).split(",")
        if item.strip()
    )
    DASH_SERVER_CONSUMPTION_MAX_ROWS: int = int(
        os.environ.get("DASH_SERVER_CONSUMPTION_MAX_ROWS", "100000")
    )
    DASH_SERVER_CONSUMPTION_MAX_BYTES: int = int(
        os.environ.get("DASH_SERVER_CONSUMPTION_MAX_BYTES", str(50 * 1024 * 1024))
    )
    DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED: bool = _env_bool(
        "DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED",
        False,
    )
    # Whole-instance override. When set (CLI `--instance-path`, env
    # `DASH_SERVER_INSTANCE_PATH`, or test_config["INSTANCE_PATH"]), every
    # un-overridden `*_ROOT` below derives from it via `Config.default_*`.
    # Falls back to `<project>/instance` when None.
    INSTANCE_PATH: str | None = os.environ.get("DASH_SERVER_INSTANCE_PATH") or os.environ.get(
        "FLASK_INSTANCE_PATH"
    )
    # Per-subroot overrides. Each falls back to its `default_*` helper anchored
    # under INSTANCE_PATH. Env-var names mirror the config keys.
    REGISTRY_DB_PATH: str | None = os.environ.get("DASH_SERVER_REGISTRY_DB_PATH")
    ARTIFACTS_ROOT: str | None = os.environ.get("DASH_SERVER_ARTIFACTS_ROOT")
    WORKSPACES_ROOT: str | None = os.environ.get("DASH_SERVER_WORKSPACES_ROOT")
    DIAGNOSTICS_ROOT: str | None = os.environ.get("DASH_SERVER_DIAGNOSTICS_ROOT")
    DEPENDENCY_STATE_ROOT: str | None = os.environ.get("DASH_SERVER_DEPENDENCY_STATE_ROOT")
    GITOPS_REPO_PATH: str | None = os.environ.get("DASH_SERVER_GITOPS_REPO_PATH")
    EXASOL_SECRETS_ROOT: str | None = os.environ.get("DASH_SERVER_EXASOL_SECRETS_ROOT")
    EXASOL_BOOTSTRAP_PROFILE_NAME: str | None = os.environ.get("DASH_SERVER_EXASOL_PROFILE_NAME")
    EXASOL_BOOTSTRAP_BACKEND: str = os.environ.get("DASH_SERVER_EXASOL_BACKEND", "onprem")
    EXASOL_BOOTSTRAP_CREDENTIAL_MODE: str = os.environ.get(
        "DASH_SERVER_EXASOL_CREDENTIAL_MODE",
        "password",
    )
    EXASOL_BOOTSTRAP_DSN: str | None = os.environ.get("DASH_SERVER_EXASOL_DSN")
    EXASOL_BOOTSTRAP_USER: str | None = os.environ.get("DASH_SERVER_EXASOL_USER")
    EXASOL_BOOTSTRAP_DESCRIPTION: str | None = os.environ.get("DASH_SERVER_EXASOL_DESCRIPTION")
    EXASOL_BOOTSTRAP_TLS_VERIFY: bool = os.environ.get(
        "DASH_SERVER_EXASOL_TLS_VERIFY",
        "true",
    ).strip().lower() not in {"0", "false", "no", "off"}
    EXASOL_BOOTSTRAP_SECRET_ENV_VAR: str | None = os.environ.get(
        "DASH_SERVER_EXASOL_SECRET_ENV_VAR",
        "EXA_PASSWORD",
    )
    EXASOL_BOOTSTRAP_STATEMENT_TIMEOUT_SECONDS: int = int(
        os.environ.get("DASH_SERVER_EXASOL_STATEMENT_TIMEOUT_SECONDS", "30")
    )
    EXASOL_BOOTSTRAP_ROW_LIMIT: int = int(
        os.environ.get("DASH_SERVER_EXASOL_ROW_LIMIT", "50000")
    )
    AUTO_INSTALL_DEPENDENCIES = True
    PYTHON_EXECUTABLE = sys.executable
    DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 120

    # --- Runtime isolation (see plans/app-runtime-isolation-and-dependency-environments-plan.md) ---
    APP_DEPENDENCY_ISOLATION: str = os.environ.get(
        "DASH_SERVER_APP_DEPENDENCY_ISOLATION", "shared"
    )  # "shared" | "per_app"
    APP_RUNTIME_MODE: str = os.environ.get(
        "DASH_SERVER_APP_RUNTIME_MODE", "in_process"
    )  # "in_process" | "isolated"
    APP_ENVIRONMENTS_ROOT: str | None = os.environ.get("DASH_SERVER_APP_ENVIRONMENTS_ROOT")
    APP_WHEEL_CACHE_ROOT: str | None = os.environ.get("DASH_SERVER_APP_WHEEL_CACHE_ROOT")
    APP_PYCACHE_ROOT: str | None = os.environ.get("DASH_SERVER_APP_PYCACHE_ROOT")
    APP_ENVIRONMENTS_DISK_CAP_GB: float = float(
        os.environ.get("DASH_SERVER_APP_ENVIRONMENTS_DISK_CAP_GB", "5.0")
    )
    APP_WHEEL_CACHE_DISK_CAP_GB: float = float(
        os.environ.get("DASH_SERVER_APP_WHEEL_CACHE_DISK_CAP_GB", "2.0")
    )
    APP_ENV_GC_RETENTION_DAYS: int = int(
        os.environ.get("DASH_SERVER_APP_ENV_GC_RETENTION_DAYS", "7")
    )
    # Phase 5: background GC drivers (off by default so existing operators see no
    # behavior change unless they opt in).
    APP_ENV_GC_ENABLED: bool = _env_bool("DASH_SERVER_APP_ENV_GC_ENABLED", False)
    APP_WHEEL_CACHE_GC_ENABLED: bool = _env_bool(
        "DASH_SERVER_APP_WHEEL_CACHE_GC_ENABLED", False
    )
    APP_ENV_GC_INTERVAL_SECONDS: int = int(
        os.environ.get("DASH_SERVER_APP_ENV_GC_INTERVAL_SECONDS", "300")
    )
    APP_WHEEL_CACHE_GC_INTERVAL_SECONDS: int = int(
        os.environ.get("DASH_SERVER_APP_WHEEL_CACHE_GC_INTERVAL_SECONDS", "600")
    )
    APP_WORKER_HOST: str = os.environ.get("DASH_SERVER_APP_WORKER_HOST", "127.0.0.1")
    APP_WORKER_PORT_RANGE: str | None = os.environ.get("DASH_SERVER_APP_WORKER_PORT_RANGE")
    APP_WORKER_PREWARM_POOL_SIZE: int = int(
        os.environ.get("DASH_SERVER_APP_WORKER_PREWARM_POOL_SIZE", "1")
    )
    APP_WORKER_PREWARM_PACKAGES: tuple[str, ...] = tuple(
        item.strip()
        for item in os.environ.get(
            "DASH_SERVER_APP_WORKER_PREWARM_PACKAGES",
            "dash,plotly,pyexasol,dash_server_runtime",
        ).split(",")
        if item.strip()
    )
    APP_WORKER_START_TIMEOUT_SECONDS: int = int(
        os.environ.get("DASH_SERVER_APP_WORKER_START_TIMEOUT_SECONDS", "30")
    )
    APP_WORKER_IDLE_STOP_SECONDS: int = int(
        os.environ.get("DASH_SERVER_APP_WORKER_IDLE_STOP_SECONDS", "600")
    )
    APP_WORKER_MAX_RESTARTS_PER_5_MINUTES: int = int(
        os.environ.get("DASH_SERVER_APP_WORKER_MAX_RESTARTS_PER_5_MINUTES", "5")
    )
    DASH_SERVER_ALLOW_UNSAFE_INPROCESS: bool = _env_bool(
        "DASH_SERVER_ALLOW_UNSAFE_INPROCESS", False
    )

    @staticmethod
    def default_app_environments_root(instance_path: str) -> str:
        return str(Path(instance_path) / "app_envs")

    @staticmethod
    def default_app_wheel_cache_root(instance_path: str) -> str:
        return str(Path(instance_path) / "wheels")

    @staticmethod
    def default_app_pycache_root(instance_path: str) -> str:
        return str(Path(instance_path) / "pycache")

    @staticmethod
    def default_db_path(instance_path: str) -> str:
        return str(Path(instance_path) / "dash_server.sqlite3")

    @staticmethod
    def default_artifacts_root(instance_path: str) -> str:
        return str(Path(instance_path) / "artifacts")

    @staticmethod
    def default_workspaces_root(instance_path: str) -> str:
        return str(Path(instance_path) / "workspaces")

    @staticmethod
    def default_diagnostics_root(instance_path: str) -> str:
        return str(Path(instance_path) / "diagnostics")

    @staticmethod
    def default_dependency_state_root(instance_path: str) -> str:
        return str(Path(instance_path) / "dependency_state")

    @staticmethod
    def default_gitops_repo_path(instance_path: str) -> str:
        return str(Path(instance_path) / "gitops-repo")

    @staticmethod
    def default_exasol_secrets_root(instance_path: str) -> str:
        return str(Path(instance_path) / "exasol-secrets")


class TestConfig(Config):
    """Base test configuration."""

    TESTING = True
    AUTO_INSTALL_DEPENDENCIES = False
