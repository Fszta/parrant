import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def dbt_artifacts():
    """Set up dbt project and return paths to artifacts."""
    # Import here to avoid circular imports
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from tests.resources.dbt_test_project.setup import setup_dbt_project

    project_dir = Path(__file__).parent.parent / "resources" / "dbt_test_project"
    return setup_dbt_project(project_dir)
