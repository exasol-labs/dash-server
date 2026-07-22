"""Flask application factory for dash-server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, g

from .auth import AuthContext, AuthorizationService, IdentityService
from .auth.blueprint import create_auth_blueprint
from .config import Config, coerce_bool
from .constants import (
    AUTH_PROVIDERS,
    DEPENDENCY_ISOLATION_MODES,
    DEPLOYMENT_MODES,
    RUNTIME_MODES,
    SAMESITE_VALUES,
)
from .consumption import ConsumptionService
from .consumption.blueprint import create_consumption_blueprint
from .dependencies import DependencyEnvironmentService, DependencyInstaller
from .diagnostics import DiagnosticsService
from .exasol import ExasolDashboardService
from .gitops import GitRepoService, GitWorktreeService
from .mailer import InvitationEmailSender
from .mcp.blueprint import create_mcp_blueprint
from .mcp.server import MCPServer
from .public.blueprint import create_public_blueprint
from .registry.sqlite_registry import SQLiteAppRegistry
from .runtime.dispatcher import DynamicPrefixDispatcher
from .runtime.service import AppRuntimeService


def _configure_deployment_mode(app: Flask) -> AuthContext:
    mode = str(app.config.get("DASH_SERVER_MODE", "local")).strip().lower()
    if mode not in DEPLOYMENT_MODES:
        raise RuntimeError("DASH_SERVER_MODE must be either 'local' or 'hosted'.")
    app.config["DASH_SERVER_MODE"] = mode

    configured_auth_enabled = app.config.get("DASH_SERVER_AUTH_ENABLED")
    if configured_auth_enabled is None:
        auth_enabled = mode == "hosted"
    else:
        auth_enabled = coerce_bool(configured_auth_enabled)
    app.config["DASH_SERVER_AUTH_ENABLED"] = auth_enabled

    configured_provider = app.config.get("DASH_SERVER_AUTH_PROVIDER")
    if configured_provider is None:
        provider = "oidc" if mode == "hosted" else "disabled"
    else:
        provider = str(configured_provider).strip().lower()
    if provider not in AUTH_PROVIDERS:
        raise RuntimeError("DASH_SERVER_AUTH_PROVIDER must be disabled, oidc, or trusted_proxy.")
    app.config["DASH_SERVER_AUTH_PROVIDER"] = provider

    if mode == "hosted":
        if not auth_enabled:
            raise RuntimeError("Hosted mode requires DASH_SERVER_AUTH_ENABLED=true.")
        if provider == "disabled":
            raise RuntimeError("Hosted mode requires DASH_SERVER_AUTH_PROVIDER to be oidc or trusted_proxy.")
        _validate_hosted_mode_config(app)
        _validate_hosted_auth_provider_config(app)

    return AuthContext.for_mode(mode, auth_enabled=auth_enabled, provider=provider)


def _validate_hosted_mode_config(app: Flask) -> None:
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Hosted mode requires SECRET_KEY or DASH_SERVER_SECRET_KEY.")
    session_cookie_secure = coerce_bool(app.config.get("SESSION_COOKIE_SECURE"))
    app.config["SESSION_COOKIE_SECURE"] = session_cookie_secure
    if not session_cookie_secure:
        raise RuntimeError("Hosted mode requires SESSION_COOKIE_SECURE=true.")
    session_cookie_httponly = coerce_bool(app.config.get("SESSION_COOKIE_HTTPONLY"), default=True)
    app.config["SESSION_COOKIE_HTTPONLY"] = session_cookie_httponly
    if not session_cookie_httponly:
        raise RuntimeError("Hosted mode requires SESSION_COOKIE_HTTPONLY=true.")
    same_site = app.config.get("SESSION_COOKIE_SAMESITE")
    if not isinstance(same_site, str) or same_site.lower() not in SAMESITE_VALUES:
        raise RuntimeError("Hosted mode requires SESSION_COOKIE_SAMESITE to be Lax, Strict, or None.")
    public_base_url = app.config.get("DASH_SERVER_PUBLIC_BASE_URL")
    if not isinstance(public_base_url, str) or not public_base_url.startswith("https://"):
        raise RuntimeError("Hosted mode requires DASH_SERVER_PUBLIC_BASE_URL to be an https:// URL.")


def _validate_runtime_isolation_config(app: Flask) -> None:
    """Validate the runtime-isolation flags (see persona-study-remediation-plan)."""

    _validate_port_config(app)

    dep_iso = str(app.config.get("APP_DEPENDENCY_ISOLATION", "shared")).strip().lower()
    runtime_mode = str(app.config.get("APP_RUNTIME_MODE", "in_process")).strip().lower()
    if dep_iso not in DEPENDENCY_ISOLATION_MODES:
        raise RuntimeError(
            "DASH_SERVER_APP_DEPENDENCY_ISOLATION must be 'shared' or 'per_app'."
        )
    if runtime_mode not in RUNTIME_MODES:
        raise RuntimeError(
            "DASH_SERVER_APP_RUNTIME_MODE must be 'in_process' or 'isolated'."
        )
    app.config["APP_DEPENDENCY_ISOLATION"] = dep_iso
    app.config["APP_RUNTIME_MODE"] = runtime_mode

    if app.config.get("DASH_SERVER_MODE") == "hosted":
        unsafe_ok = coerce_bool(app.config.get("DASH_SERVER_ALLOW_UNSAFE_INPROCESS"))
        if (dep_iso != "per_app" or runtime_mode != "isolated") and not unsafe_ok:
            raise RuntimeError(
                "Hosted mode requires APP_DEPENDENCY_ISOLATION=per_app and "
                "APP_RUNTIME_MODE=isolated. Set DASH_SERVER_ALLOW_UNSAFE_INPROCESS=true "
                "to override during development."
            )
        # Phase 5d: when the override is honored, log loudly every startup so operators
        # see it. Pair with a runtime.events event so the override usage is auditable via
        # dash://runtime/logs/runtime.events.
        if unsafe_ok and (dep_iso != "per_app" or runtime_mode != "isolated"):
            app.logger.warning(
                "DASH_SERVER_ALLOW_UNSAFE_INPROCESS=true is honoring "
                "APP_DEPENDENCY_ISOLATION=%s / APP_RUNTIME_MODE=%s in hosted mode. "
                "Hosted mode normally requires per_app + isolated; this override is a "
                "development-only escape hatch and should not be used in production.",
                dep_iso,
                runtime_mode,
            )


def _validate_port_config(app: Flask) -> None:
    control_plane_port = _coerce_port(
        app.config.get("DASH_SERVER_PORT", 5100),
        key="DASH_SERVER_PORT",
    )
    app.config["DASH_SERVER_PORT"] = control_plane_port

    worker_range = app.config.get("APP_WORKER_PORT_RANGE")
    if worker_range is None or str(worker_range).strip() == "":
        app.config["APP_WORKER_PORT_RANGE"] = None
        return
    start, end = _parse_port_range(str(worker_range), key="DASH_SERVER_APP_WORKER_PORT_RANGE")
    app.config["APP_WORKER_PORT_RANGE"] = f"{start}-{end}"


def _coerce_port(value: Any, *, key: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer port.") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{key} must be between 1 and 65535.")
    return port


def _parse_port_range(value: str, *, key: str) -> tuple[int, int]:
    if "-" not in value:
        raise RuntimeError(f"{key} must use START-END syntax.")
    start_text, end_text = (part.strip() for part in value.split("-", 1))
    start = _coerce_port(start_text, key=key)
    end = _coerce_port(end_text, key=key)
    if start > end:
        raise RuntimeError(f"{key} start must be less than or equal to end.")
    return start, end


def _validate_hosted_auth_provider_config(app: Flask) -> None:
    provider = app.config["DASH_SERVER_AUTH_PROVIDER"]
    if provider == "oidc":
        for key in (
            "DASH_SERVER_OIDC_ISSUER",
            "DASH_SERVER_OIDC_CLIENT_ID",
            "DASH_SERVER_OIDC_REDIRECT_URI",
        ):
            value = app.config.get(key)
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"Hosted OIDC auth requires {key}.")
        redirect_uri = app.config["DASH_SERVER_OIDC_REDIRECT_URI"]
        if not str(redirect_uri).startswith("https://"):
            raise RuntimeError("Hosted OIDC auth requires DASH_SERVER_OIDC_REDIRECT_URI to be an https:// URL.")
        return

    if provider == "trusted_proxy":
        if not coerce_bool(app.config.get("DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED")):
            raise RuntimeError("Hosted trusted_proxy auth requires DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED=true.")
        allowed_cidrs = app.config.get("DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS")
        if not _config_string_sequence(allowed_cidrs):
            raise RuntimeError("Hosted trusted_proxy auth requires DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS.")


def _config_string_sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _resolve_instance_path(test_config: dict[str, Any] | None, project_root: Path) -> str:
    """Pick the instance directory. See `create_app` for the precedence order."""

    if test_config and test_config.get("INSTANCE_PATH"):
        return str(test_config["INSTANCE_PATH"])
    env_override = os.environ.get("DASH_SERVER_INSTANCE_PATH") or os.environ.get(
        "FLASK_INSTANCE_PATH"
    )
    if env_override:
        return env_override
    return str(project_root / "instance")


def _bootstrap_exasol_profile(app: Flask, exasol_dashboard_service: ExasolDashboardService) -> None:
    profile_name = app.config.get("EXASOL_BOOTSTRAP_PROFILE_NAME")
    if not profile_name:
        return

    existing_names = {profile.name for profile in exasol_dashboard_service.profile_store.list_profiles()}
    if profile_name in existing_names:
        # BUG-012 fix: persona-1 reported that restarting with new DSN/description env
        # vars silently kept the old profile. Surface the no-op so operators don't
        # think the new bootstrap values applied.
        app.logger.info(
            "exasol bootstrap: profile %s already exists; not modifying. "
            "Use exasol_profile_create_local with overwrite=true to rewrite.",
            profile_name,
        )
        return

    dsn = app.config.get("EXASOL_BOOTSTRAP_DSN")
    user = app.config.get("EXASOL_BOOTSTRAP_USER")
    if not dsn or not user:
        raise RuntimeError(
            "EXASOL bootstrap profile is enabled but DASH_SERVER_EXASOL_DSN or "
            "DASH_SERVER_EXASOL_USER is missing."
        )

    secret_env_var = app.config.get("EXASOL_BOOTSTRAP_SECRET_ENV_VAR")
    if not secret_env_var:
        raise RuntimeError(
            "EXASOL bootstrap profile is enabled but DASH_SERVER_EXASOL_SECRET_ENV_VAR is missing."
        )

    exasol_dashboard_service.create_local_profile(
        name=str(profile_name),
        backend=str(app.config.get("EXASOL_BOOTSTRAP_BACKEND", "onprem")),
        credential_mode=str(app.config.get("EXASOL_BOOTSTRAP_CREDENTIAL_MODE", "password")),
        dsn=str(dsn),
        user=str(user),
        description=app.config.get("EXASOL_BOOTSTRAP_DESCRIPTION"),
        tls_verify=bool(app.config.get("EXASOL_BOOTSTRAP_TLS_VERIFY", True)),
        secret_env_var=str(secret_env_var),
        statement_timeout_seconds=int(app.config.get("EXASOL_BOOTSTRAP_STATEMENT_TIMEOUT_SECONDS", 30)),
        row_limit=int(app.config.get("EXASOL_BOOTSTRAP_ROW_LIMIT", 50000)),
    )


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """Create and configure the Flask control-plane app.

    Instance-path resolution (highest priority first):
      1. ``test_config["INSTANCE_PATH"]`` — explicit pytest / programmatic override.
      2. ``DASH_SERVER_INSTANCE_PATH`` or ``FLASK_INSTANCE_PATH`` env var.
      3. Default: ``<project_root>/instance``.

    Per-subroot keys (``REGISTRY_DB_PATH``, ``ARTIFACTS_ROOT``, etc.) each have their
    own ``DASH_SERVER_*`` env-var fallback in ``Config`` and override the per-instance
    derivation when set.
    """

    project_root = Path(__file__).resolve().parents[2]
    instance_path = _resolve_instance_path(test_config, project_root)
    app = Flask(
        __name__,
        instance_path=instance_path,
        instance_relative_config=True,
    )
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)
    # Echo the resolved path back onto config for downstream code (and tests) that
    # want to know which directory the run is anchored under.
    app.config["INSTANCE_PATH"] = instance_path

    auth_context = _configure_deployment_mode(app)
    email_sender = InvitationEmailSender(app.config)
    email_sender.validate_startup(hosted_mode=auth_context.mode == "hosted")

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db_path = app.config["REGISTRY_DB_PATH"] or Config.default_db_path(app.instance_path)
    artifacts_root = app.config["ARTIFACTS_ROOT"] or Config.default_artifacts_root(app.instance_path)
    workspaces_root = app.config["WORKSPACES_ROOT"] or Config.default_workspaces_root(app.instance_path)
    diagnostics_root = app.config["DIAGNOSTICS_ROOT"] or Config.default_diagnostics_root(
        app.instance_path
    )
    dependency_state_root = app.config["DEPENDENCY_STATE_ROOT"] or Config.default_dependency_state_root(
        app.instance_path
    )
    gitops_repo_path = app.config["GITOPS_REPO_PATH"] or Config.default_gitops_repo_path(app.instance_path)
    exasol_secrets_root = app.config["EXASOL_SECRETS_ROOT"] or Config.default_exasol_secrets_root(
        app.instance_path
    )
    app_environments_root = app.config["APP_ENVIRONMENTS_ROOT"] or Config.default_app_environments_root(
        app.instance_path
    )
    app_wheel_cache_root = app.config["APP_WHEEL_CACHE_ROOT"] or Config.default_app_wheel_cache_root(
        app.instance_path
    )
    app_pycache_root = app.config["APP_PYCACHE_ROOT"] or Config.default_app_pycache_root(
        app.instance_path
    )
    app.config["APP_ENVIRONMENTS_ROOT"] = app_environments_root
    app.config["APP_WHEEL_CACHE_ROOT"] = app_wheel_cache_root
    app.config["APP_PYCACHE_ROOT"] = app_pycache_root
    _validate_runtime_isolation_config(app)
    git_repo_service = GitRepoService(gitops_repo_path)
    git_repo_service.initialize()
    git_worktree_service = GitWorktreeService(git_repo_service, workspaces_root)
    registry = SQLiteAppRegistry(db_path)
    registry.initialize()

    dispatcher = DynamicPrefixDispatcher(app.wsgi_app)
    # Flask exposes `wsgi_app` as a method on the class but documents reassigning it
    # for middleware (see https://flask.palletsprojects.com/en/latest/api/#flask.Flask.wsgi_app),
    # which is exactly what we're doing here. The type stubs don't model that idiom.
    app.wsgi_app = dispatcher  # type: ignore[method-assign]

    diagnostics_service = DiagnosticsService(diagnostics_root)

    # Phase 1: when APP_DEPENDENCY_ISOLATION=per_app, hosted apps' requirements install into
    # a per-(dependency_lock_hash) venv under instance/app_envs/, not the server interpreter.
    dependency_environment_service: DependencyEnvironmentService | None = None
    if app.config.get("APP_DEPENDENCY_ISOLATION") == "per_app":
        helper_source = Path(__file__).resolve().parent.parent / "dash_server_runtime"
        # Phase 5b config: read GC settings from app.config so they can be overridden in tests.
        env_gc_enabled = bool(app.config.get("APP_ENV_GC_ENABLED", False))
        wheel_gc_enabled = bool(app.config.get("APP_WHEEL_CACHE_GC_ENABLED", False))
        env_gc_interval = float(app.config.get("APP_ENV_GC_INTERVAL_SECONDS", 300))
        wheel_gc_interval = float(app.config.get("APP_WHEEL_CACHE_GC_INTERVAL_SECONDS", 600))
        env_retention_seconds = float(app.config.get("APP_ENV_GC_RETENTION_DAYS", 7)) * 24 * 3600
        env_disk_cap_gb = float(app.config.get("APP_ENVIRONMENTS_DISK_CAP_GB", 5.0))
        env_disk_cap_bytes = int(env_disk_cap_gb * (1024 ** 3))

        dependency_environment_service = DependencyEnvironmentService(
            environments_root=app_environments_root,
            wheel_cache_root=app_wheel_cache_root,
            pycache_root=app_pycache_root,
            enabled=bool(app.config["AUTO_INSTALL_DEPENDENCIES"]),
            base_python_executable=app.config["PYTHON_EXECUTABLE"],
            timeout_seconds=int(app.config["DEPENDENCY_INSTALL_TIMEOUT_SECONDS"]),
            helper_package_source=helper_source if helper_source.exists() else None,
            backend="venv",  # Phase 5a: stdlib backend; wheel-cache GC is no-op until uv lands
            diagnostics_service=diagnostics_service,
            env_gc_enabled=env_gc_enabled,
            wheel_cache_gc_enabled=wheel_gc_enabled,
            env_gc_interval_seconds=env_gc_interval,
            wheel_cache_gc_interval_seconds=wheel_gc_interval,
            env_retention_seconds=env_retention_seconds,
            disk_cap_bytes=env_disk_cap_bytes,
        )
        # Reference-set provider: env GC asks the registry which env ids are still live.
        dependency_environment_service.referenced_ids_provider = (
            registry.list_referenced_environment_ids
        )
        # Background drivers respect their off-switches (default off).
        dependency_environment_service.start_env_gc()
        dependency_environment_service.start_wheel_cache_gc()
        dependency_installer: Any = dependency_environment_service
    else:
        dependency_installer = DependencyInstaller(
            dependency_state_root,
            enabled=bool(app.config["AUTO_INSTALL_DEPENDENCIES"]),
            python_executable=app.config["PYTHON_EXECUTABLE"],
            timeout_seconds=int(app.config["DEPENDENCY_INSTALL_TIMEOUT_SECONDS"]),
        )
    exasol_dashboard_service = ExasolDashboardService(git_repo_service, exasol_secrets_root)
    _bootstrap_exasol_profile(app, exasol_dashboard_service)

    runtime_mode = str(app.config.get("APP_RUNTIME_MODE", "in_process")).strip().lower()
    worker_manager = None
    if runtime_mode == "isolated":
        from .runtime.worker_manager import AppWorkerManager

        workers_root = str(Path(app.instance_path) / "workers")
        prewarm_pool_size = int(app.config.get("APP_WORKER_PREWARM_POOL_SIZE", 1))
        prewarm_packages = tuple(app.config.get("APP_WORKER_PREWARM_PACKAGES") or ()) or (
            "dash",
            "flask",
            "dash_server_runtime",
        )
        worker_manager = AppWorkerManager(
            workers_root=workers_root,
            diagnostics_root=diagnostics_root,
            gitops_repo_path=gitops_repo_path,
            exasol_secrets_root=exasol_secrets_root,
            pycache_root=app_pycache_root,
            start_timeout_seconds=int(app.config.get("APP_WORKER_START_TIMEOUT_SECONDS", 30)),
            idle_stop_seconds=int(app.config.get("APP_WORKER_IDLE_STOP_SECONDS", 600)),
            host=str(app.config.get("APP_WORKER_HOST", "127.0.0.1")),
            port_range=app.config.get("APP_WORKER_PORT_RANGE"),
            diagnostics_service=diagnostics_service,
            enable_forkserver=prewarm_pool_size > 0,
            prewarm_packages=prewarm_packages,
            max_restarts_per_5_minutes=int(
                app.config.get("APP_WORKER_MAX_RESTARTS_PER_5_MINUTES", 5)
            ),
        )
        # Start the idle sweep so workers without traffic for APP_WORKER_IDLE_STOP_SECONDS
        # are stopped and persisted as `stopped_idle`. ensure_running re-spawns transparently
        # on the next request.
        worker_manager.start_idle_sweep()

    runtime_service = AppRuntimeService(
        registry,
        dispatcher,
        artifacts_root,
        workspaces_root,
        diagnostics_service,
        dependency_installer,
        git_repo_service,
        git_worktree_service,
        runtime_extensions={
            "exasol_dashboard_service": exasol_dashboard_service,
            "diagnostics_service": diagnostics_service,
        },
        worker_manager=worker_manager,
        runtime_mode=runtime_mode,
        dependency_environment_service=dependency_environment_service,
    )
    if not git_repo_service.has_commits():
        runtime_service.ensure_demo_app()
        git_repo_service.ensure_phase0_repository(
            runtime_service.workspace_service.read_all_files("demo")
        )
    for hosted_app in registry.list_apps():
        runtime_service.workspace_service.ensure_workspace_backend(hosted_app.name)
    runtime_service.backfill_revision_git_metadata()
    runtime_service.rebuild_cache_from_git()
    runtime_service.bootstrap()
    runtime_service.reconcile_git_desired_state()

    app.extensions["dispatcher"] = dispatcher
    app.extensions["registry"] = registry
    app.extensions["diagnostics_service"] = diagnostics_service
    app.extensions["dependency_installer"] = dependency_installer
    if dependency_environment_service is not None:
        app.extensions["dependency_environment_service"] = dependency_environment_service
    if worker_manager is not None:
        app.extensions["worker_manager"] = worker_manager
    app.extensions["exasol_dashboard_service"] = exasol_dashboard_service
    app.extensions["runtime_service"] = runtime_service
    app.extensions["git_repo_service"] = git_repo_service
    app.extensions["git_worktree_service"] = git_worktree_service
    app.extensions["auth_context"] = auth_context
    app.extensions["email_sender"] = email_sender
    identity_service = IdentityService(app.config)
    authorization_service = AuthorizationService(registry, app.config)
    consumption_service = ConsumptionService(
        registry,
        authorization_service,
        app.config,
        exasol_service=exasol_dashboard_service,
        artifacts_root=artifacts_root,
    )
    consumption_service.start()
    app.extensions["identity_service"] = identity_service
    app.extensions["authorization_service"] = authorization_service
    app.extensions["consumption_service"] = consumption_service
    app.extensions["mcp_server"] = MCPServer(
        runtime_service,
        git_repo_service,
        exasol_dashboard_service=exasol_dashboard_service,
        email_sender=email_sender,
        consumption_service=consumption_service,
    )

    def _authorize_mounted_dashboard(
        environ: dict[str, Any],
        mount_prefix: str,
    ) -> tuple[str, list[tuple[str, str]], bytes] | None:
        with app.request_context(environ):
            resolved_context = identity_service.context_for_request()
            decision = authorization_service.authorize_path(
                resolved_context,
                path=str(environ.get("PATH_INFO") or "/"),
                mount_prefix=mount_prefix,
            )
        if decision.allowed:
            return None
        return authorization_service.denial_wsgi_response(decision)

    dispatcher.set_authorization_handler(_authorize_mounted_dashboard)

    @app.before_request
    def _load_auth_context() -> None:
        g.auth_context = identity_service.context_for_request()

    app.register_blueprint(create_auth_blueprint())
    app.register_blueprint(create_mcp_blueprint())
    app.register_blueprint(create_public_blueprint())
    app.register_blueprint(create_consumption_blueprint())

    return app
