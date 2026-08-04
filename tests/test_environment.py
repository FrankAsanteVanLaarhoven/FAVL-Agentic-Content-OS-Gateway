"""Reproducibility of the test environment itself.

This host carries ROS 2 Humble on the global PYTHONPATH, and pytest autoloads
plugins from every distribution it can see. That silently pulled ROS's
launch_testing into repository test runs. These checks fail loudly if the
isolation regresses, rather than leaving a confusing import error.

Nothing is sanitised speculatively: only PYTHONPATH is cleared, because only
PYTHONPATH was shown to contaminate the suite.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Third-party distributions allowed to register a pytest plugin. Anything
# else reaching the run came from outside requirements-dev.txt.
ALLOWED_PLUGIN_DISTRIBUTIONS = {"pytest-asyncio", "anyio"}


def test_tests_run_from_the_repository_local_venv():
    assert str(ROOT / ".venv") in sys.prefix, (
        f"tests must run from {ROOT / '.venv'}, not {sys.prefix}. Use `make test`."
    )


def test_no_external_python_path_leaks_in():
    """ROS sets PYTHONPATH globally; make test clears it."""
    leaked = os.environ.get("PYTHONPATH", "")
    assert not leaked, (
        f"PYTHONPATH is set to {leaked!r}; external site-packages can inject "
        "pytest plugins into this run."
    )


def test_no_unexpected_pytest_plugins_are_loaded(pytestconfig):
    """Only distributions from requirements-dev.txt may register plugins.

    `list_plugin_distinfo` reports exactly the entry-point plugins — the
    channel ROS used — and excludes pytest's own internals.
    """
    installed = {
        dist.project_name
        for _, dist in pytestconfig.pluginmanager.list_plugin_distinfo()
    }
    unexpected = sorted(installed - ALLOWED_PLUGIN_DISTRIBUTIONS)
    assert not unexpected, (
        f"pytest plugins from unexpected distributions: {unexpected}. "
        "Something outside requirements-dev.txt is on sys.path."
    )


def test_no_ros_paths_on_sys_path():
    ros = [p for p in sys.path if "/opt/ros" in p]
    assert not ros, f"ROS paths leaked onto sys.path: {ros}"
