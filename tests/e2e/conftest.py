"""Conftest for e2e tests - reuses integration conftest."""

import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="session")
def dbt_artifacts() -> dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from tests.resources.dbt_test_project.setup import setup_dbt_project

    project_dir = Path(__file__).parent.parent / "resources" / "dbt_test_project"
    return setup_dbt_project(project_dir)
