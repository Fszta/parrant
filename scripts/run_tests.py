import os
import subprocess
import sys
from pathlib import Path


def run_tests(test_type: str | None = None) -> int:
    """Run tests with pytest."""
    project_root = Path(__file__).parent.parent
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [os.environ.get("PYTHONPATH", ""), str(project_root)]
    )

    # Invoke pytest via the current interpreter (`python -m pytest`) rather than the bare
    # `pytest` binary, so it doesn't depend on the venv's bin dir being on the subprocess PATH
    # (which isn't guaranteed in every CI runner / cached-venv state).
    pytest_cmd = [sys.executable, "-m", "pytest"]
    if test_type == "unit":
        cmd = [*pytest_cmd, "tests/unit", "-v"]
    elif test_type == "integration":
        cmd = [*pytest_cmd, "tests/integration", "-v"]
    elif test_type == "e2e":
        cmd = [*pytest_cmd, "tests/e2e", "-v"]
    else:
        cmd = [*pytest_cmd, "tests", "-v"]

    result = subprocess.run(cmd, cwd=project_root)
    return result.returncode


def run_tests_unit() -> int:
    return run_tests("unit")


def run_tests_integration() -> int:
    return run_tests("integration")


def run_tests_e2e() -> int:
    return run_tests("e2e")


if __name__ == "__main__":
    test_type = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(run_tests(test_type))
