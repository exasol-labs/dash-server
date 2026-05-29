"""Helpers to isolate Dash global callback registries during dynamic loads."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from dash import Dash
from dash import _callback as dash_callback_module


@contextmanager
def isolated_dash_callback_globals() -> Iterator[None]:
    """Run code with a fresh Dash global callback registry and restore prior state after."""

    saved_callback_map = dict(dash_callback_module.GLOBAL_CALLBACK_MAP)
    saved_callback_list = list(dash_callback_module.GLOBAL_CALLBACK_LIST)
    saved_inline_scripts = list(dash_callback_module.GLOBAL_INLINE_SCRIPTS)
    saved_api_paths = dict(dash_callback_module.GLOBAL_API_PATHS)

    dash_callback_module.GLOBAL_CALLBACK_MAP.clear()
    dash_callback_module.GLOBAL_CALLBACK_LIST.clear()
    dash_callback_module.GLOBAL_INLINE_SCRIPTS.clear()
    dash_callback_module.GLOBAL_API_PATHS.clear()
    try:
        yield
    finally:
        dash_callback_module.GLOBAL_CALLBACK_MAP.clear()
        dash_callback_module.GLOBAL_CALLBACK_MAP.update(saved_callback_map)
        dash_callback_module.GLOBAL_CALLBACK_LIST.clear()
        dash_callback_module.GLOBAL_CALLBACK_LIST.extend(saved_callback_list)
        dash_callback_module.GLOBAL_INLINE_SCRIPTS.clear()
        dash_callback_module.GLOBAL_INLINE_SCRIPTS.extend(saved_inline_scripts)
        dash_callback_module.GLOBAL_API_PATHS.clear()
        dash_callback_module.GLOBAL_API_PATHS.update(saved_api_paths)


def finalize_dash_app_callbacks(dash_app: Dash) -> None:
    """Force Dash to consume any global callbacks while still inside an isolated registry scope."""

    dash_app._setup_server()
