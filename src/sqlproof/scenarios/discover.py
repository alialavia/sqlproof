"""Find scenarios in a project, under one directory (default `scenarios/`).

Two kinds of file hold them:

- `*_scenarios.py`: imported by path, the way pytest collects tests;
  every module-level `Scenario` object is collected.
- `*.scenario.yaml`, `*.scenario.yml`, `*.scenario.json`: one scenario
  in its serialized form each (see `serialize.py`).

Scenario names must be unique across the directory, whichever kind of
file defines them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scenarios.model import Scenario
from sqlproof.scenarios.serialize import SCENARIO_FILE_SUFFIXES, load_file

DEFAULT_DIRECTORY = Path("scenarios")


def discover(directory: str | Path = DEFAULT_DIRECTORY) -> list[Scenario]:
    """Every scenario defined in scenario files under `directory`, sorted
    by name. Raises when two scenarios share a name."""
    root = Path(directory)
    if not root.is_dir():
        msg = (
            f"No scenarios directory at {root}. Pass the directory that holds "
            "*_scenarios.py or *.scenario.yaml / *.scenario.json files."
        )
        raise SqlProofScenarioError(msg)
    found: dict[str, Scenario] = {}
    paths = [*root.rglob("*_scenarios.py")]
    for suffix in SCENARIO_FILE_SUFFIXES:
        paths.extend(root.rglob(f"*{suffix}"))
    for path in sorted(paths):
        in_file = _scenarios_in(path) if path.suffix == ".py" else [load_file(path)]
        for scenario in in_file:
            existing = found.get(scenario.name)
            if existing is not None:
                msg = (
                    f"Scenario name {scenario.name!r} is defined twice: in "
                    f"{existing.source} and {path}."
                )
                raise SqlProofScenarioError(msg)
            found[scenario.name] = scenario
    return [found[name] for name in sorted(found)]


def find(name: str, directory: str | Path = DEFAULT_DIRECTORY) -> Scenario:
    scenarios = discover(directory)
    for scenario in scenarios:
        if scenario.name == name:
            return scenario
    known = ", ".join(s.name for s in scenarios) or "none"
    msg = f"No scenario named {name!r} in {directory}. Known: {known}."
    raise SqlProofScenarioError(msg)


def _scenarios_in(path: Path) -> list[Scenario]:
    resolved = path.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    module_name = f"_sqlproof_scenarios_{resolved.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        msg = f"Cannot import {path}."
        raise SqlProofScenarioError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Let a scenarios file import helpers that sit next to it.
    sys.path.insert(0, str(resolved.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(resolved.parent))
    scenarios: list[Scenario] = []
    for value in vars(module).values():
        if isinstance(value, Scenario):
            value.source = str(path)
            scenarios.append(value)
    return scenarios
