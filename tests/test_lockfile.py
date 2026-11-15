import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def test_all_declared_dependencies_are_pinned():
    project_root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((project_root / "pyproject.toml").read_text())
    lock_lines = (project_root / "requirements.lock").read_text().splitlines()
    pinned: dict[str, Version] = {}
    for line in lock_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        package, separator, version = line.partition("==")
        assert separator, f"Lock entry is not pinned exactly: {line}"
        requirement = Requirement(package)
        pinned[canonicalize_name(requirement.name)] = Version(version)

    dependencies = project["project"]["dependencies"]
    dependencies.extend(project["project"]["optional-dependencies"]["dev"])
    for dependency in dependencies:
        requirement = Requirement(dependency)
        installed = pinned.get(canonicalize_name(requirement.name))
        assert installed is not None, f"{requirement.name} is missing from requirements.lock"
        assert installed in requirement.specifier, (
            f"requirements.lock pins {requirement.name}=={installed}, outside "
            f"the declared range {requirement.specifier}"
        )
