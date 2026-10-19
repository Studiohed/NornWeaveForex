"""Import smoke test: every module under ``nornweave_forex`` must import.

This is deliberately blunt. The port's worst regressions were not subtle logic
bugs; they were module-level drift that made whole entry points unusable:

* ``PaperBroker`` was listed in ``live/broker.py:__all__`` but never defined, so
  importing ``live.runner`` (and therefore ``cli``) raised ``ImportError``;
* ``read_dow`` was imported by the runner and the backtester but did not exist in
  ``strategy/dow.py``;
* the consumers called shared APIs with the wrong signatures — those are not
  import-time errors, but a test that imports *everything* and builds the CLI
  parser is the cheapest tripwire for the class of failure above.

The walk covers packages recursively, so a new module is picked up automatically.
Optional third-party imports (the ``dashboard`` and ``mt5server`` extras declared
in ``pyproject.toml``) are skipped only when the dependency is genuinely not
installed; an intra-package import failure is never skipped.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path
from typing import Iterable

import pytest

import nornweave_forex

REPO_ROOT = Path(__file__).resolve().parents[1]
FOREX_YAML = REPO_ROOT / "configs" / "forex.yaml"

#: Modules that must be discovered for the parametrisation to be meaningful.
CORE_MODULES = {
    "nornweave_forex.config",
    "nornweave_forex.execution",
    "nornweave_forex.types",
    "nornweave_forex.instrument",
    "nornweave_forex.cli",
    "nornweave_forex.live.runner",
    "nornweave_forex.live.state",
    "nornweave_forex.backtest.engine",
    "nornweave_forex.strategy.engine",
    "nornweave_forex.risk.sizing",
}


def _discover() -> tuple[list[str], list[str]]:
    errors: list[str] = []

    def onerror(name: str) -> None:  # pragma: no cover - reported by a test below
        errors.append(name)

    names = [
        module.name
        for module in pkgutil.walk_packages(
            nornweave_forex.__path__,
            prefix=nornweave_forex.__name__ + ".",
            onerror=onerror,
        )
    ]
    return sorted(names), errors


MODULES, WALK_ERRORS = _discover()


def _missing_optional_dependency(exc: ModuleNotFoundError) -> str | None:
    """Return the missing third-party package, or None when this is a real failure.

    ``nornweave_forex`` importing its own missing/renamed submodule must never be
    skipped — that is the bug this file exists to catch.
    """
    name = (exc.name or "").split(".")[0]
    if not name or name == "nornweave_forex":
        return None
    if importlib.util.find_spec(name) is not None:
        return None
    return name


# --------------------------------------------------------------------------- #
# discovery is not silently empty
# --------------------------------------------------------------------------- #
def test_the_module_walk_covers_the_package():
    assert not WALK_ERRORS, f"pkgutil could not recurse into: {WALK_ERRORS}"
    assert len(MODULES) >= 25, f"only discovered {len(MODULES)} modules: {MODULES}"


def test_the_walk_finds_every_entry_point():
    missing = sorted(CORE_MODULES - set(MODULES))
    assert not missing, f"pkgutil did not discover {missing}"


# --------------------------------------------------------------------------- #
# every module imports
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str):
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        dependency = _missing_optional_dependency(exc)
        if dependency is not None:
            pytest.skip(
                f"{module_name} needs optional dependency {dependency!r} "
                "(declared in pyproject [project.optional-dependencies]); not installed"
            )
        raise


# --------------------------------------------------------------------------- #
# the two entry points the operator actually uses
# --------------------------------------------------------------------------- #
def test_cli_builds_its_parser():
    cli = importlib.import_module("nornweave_forex.cli")
    parser = cli.build_parser()
    assert parser is not None


def test_cli_parses_the_documented_commands():
    cli = importlib.import_module("nornweave_forex.cli")
    parser = cli.build_parser()
    for command in ("presets", "fetch", "backtest", "live", "demo"):
        assert parser.parse_args([command]).command == command

    backtest = parser.parse_args(["backtest"])
    # The shipped config must be the default for the real entry points.
    assert backtest.config.endswith("forex.yaml")
    live = parser.parse_args(["live"])
    assert live.config.endswith("forex.yaml")


def test_the_shipped_config_loads_through_the_public_api():
    from nornweave_forex.config import Config

    cfg = Config.from_yaml(str(FOREX_YAML))
    assert cfg.portfolio.symbols, "the watchlist must not be empty"


def test_importing_every_module_does_not_create_state_on_disk():
    """Importing the package must not have side effects such as writing state files."""
    stray = [p.name for p in REPO_ROOT.glob("state*.json") if p.is_file()]
    assert not stray, f"module import created state files: {stray}"


@pytest.fixture(scope="module", autouse=True)
def _import_all_once() -> Iterable[None]:
    """Warm every importable module once so a single broken one cannot hide the rest."""
    for module_name in MODULES:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        except ImportError:  # pragma: no cover - surfaced by the parametrised test
            continue
    yield
