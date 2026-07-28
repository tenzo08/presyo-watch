"""Scaffold smoke tests.

These assert only that the package is importable and installed in editable form —
enough to prove the toolchain is wired up before any real logic exists.
"""

import importlib.metadata

import presyowatch


def test_package_exposes_version() -> None:
    assert presyowatch.__version__ == "0.1.0"


def test_installed_distribution_version_matches_package() -> None:
    """Guards against `pyproject.toml` and `__init__.py` drifting apart."""
    assert importlib.metadata.version("presyowatch") == presyowatch.__version__
