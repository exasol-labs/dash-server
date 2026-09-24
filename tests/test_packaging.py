from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "dash_server"


def test_every_non_python_package_file_is_declared_as_package_data():
    """A file setuptools doesn't ship works from a checkout but breaks a wheel install.

    Guards against e.g. the Flask templates going missing, which made ``/`` return 500
    with ``TemplateNotFound`` on anything but an editable install.
    """

    # setuptools' own reader (bundles a TOML parser, so it works on 3.10 without tomllib).
    pyprojecttoml = pytest.importorskip("setuptools.config.pyprojecttoml")
    config = pyprojecttoml.read_configuration(str(PROJECT_ROOT / "pyproject.toml"))
    package_data: dict[str, list[str]] = config["tool"]["setuptools"]["package-data"]

    undeclared = []
    for path in PACKAGE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix in {".py", ".pyc"} or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(PACKAGE_ROOT.parent)
        covered = False
        for package, patterns in package_data.items():
            package_dir = Path(*package.split("."))
            if package_dir not in relative.parents:
                continue
            inner = relative.relative_to(package_dir)
            if any(inner.match(pattern) and len(inner.parts) == len(Path(pattern).parts) for pattern in patterns):
                covered = True
                break
        if not covered:
            undeclared.append(str(relative))

    assert undeclared == [], f"Add these to [tool.setuptools.package-data]: {undeclared}"
