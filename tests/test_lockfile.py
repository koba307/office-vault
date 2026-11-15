import re
import tomllib
from pathlib import Path


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_all_declared_dependencies_are_pinned():
    project_root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((project_root / "pyproject.toml").read_text())
    lock_lines = (project_root / "requirements.lock").read_text().splitlines()
    pinned = {
        _canonical(line.split("==", 1)[0])
        for line in lock_lines
        if line.strip() and not line.lstrip().startswith("#") and "==" in line
    }

    dependencies = project["project"]["dependencies"]
    dependencies.extend(project["project"]["optional-dependencies"]["dev"])
    declared = {
        _canonical(match.group(1))
        for dependency in dependencies
        if (match := re.match(r"([A-Za-z0-9_.-]+)", dependency))
    }

    assert declared <= pinned
