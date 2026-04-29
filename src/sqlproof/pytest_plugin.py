from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("sqlproof")
    group.addoption("--sqlproof-seed", action="store", type=int, help="Fix the SqlProof seed.")
    group.addoption(
        "--sqlproof-runs", action="store", type=int, help="Override SqlProof run count."
    )
    group.addoption(
        "--sqlproof-show-counterexample",
        action="store_true",
        help="Print full SqlProof counterexamples.",
    )
    group.addoption("--sqlproof-coverage", action="store_true", help="Enable PL/pgSQL coverage.")
    group.addoption(
        "--sqlproof-diversity-report",
        action="store_true",
        help="Print generator diversity report.",
    )
    group.addoption("--sqlproof-postgres-image", action="store", help="Override Postgres image.")
    group.addoption("--sqlproof-verbose", action="store_true", help="Enable DEBUG logging.")
