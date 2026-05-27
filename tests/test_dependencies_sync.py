"""
Test that pyproject.toml dependencies are reflected in requirements.txt / requirements-dev.txt.

Rules:
- Every package listed in [project].dependencies must have a pinned entry in requirements.txt.
- Every package listed in [project.optional-dependencies].dev must have a pinned entry in requirements-dev.txt.
- Packages in requirements.txt that are NOT listed in pyproject.toml are allowed (transitive deps).
"""

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _normalize(name: str) -> str:
    """Normalize package name: lowercase, replace _ and . with -."""
    return re.sub(r"[_.]", "-", name.strip().lower())


def _parse_pyproject_deps(section: list[str]) -> set[str]:
    """Extract normalized package names from a list of PEP 508 dependency strings."""
    names = set()
    for dep in section:
        # Strip extras like [http2], version specifiers, and whitespace
        match = re.match(r"^([A-Za-z0-9_.\-]+)", dep.strip())
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _parse_requirements(path: Path) -> set[str]:
    """Extract normalized package names from a requirements file (pinned or not)."""
    names = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        # Skip comments, empty lines, and options
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Match name at start of line (before ==, >=, [, space, etc.)
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def test_runtime_deps_in_requirements_txt():
    """All [project].dependencies must appear in requirements.txt."""
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    requirements_path = PROJECT_ROOT / "requirements.txt"

    assert pyproject_path.exists(), "pyproject.toml not found"
    assert requirements_path.exists(), "requirements.txt not found"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    project_deps = data.get("project", {}).get("dependencies", [])
    pyproject_names = _parse_pyproject_deps(project_deps)
    req_names = _parse_requirements(requirements_path)

    missing = pyproject_names - req_names
    assert not missing, (
        f"Packages in pyproject.toml [project].dependencies "
        f"but missing from requirements.txt: {sorted(missing)}"
    )


def test_dev_deps_in_requirements_dev_txt():
    """All [project.optional-dependencies].dev must appear in requirements-dev.txt."""
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    requirements_dev_path = PROJECT_ROOT / "requirements-dev.txt"

    assert pyproject_path.exists(), "pyproject.toml not found"
    assert requirements_dev_path.exists(), "requirements-dev.txt not found"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    dev_deps = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    dev_names = _parse_pyproject_deps(dev_deps)
    req_dev_names = _parse_requirements(requirements_dev_path)

    missing = dev_names - req_dev_names
    assert not missing, (
        f"Packages in pyproject.toml [project.optional-dependencies].dev "
        f"but missing from requirements-dev.txt: {sorted(missing)}"
    )


def _parse_requirements_versions(path: Path) -> dict[str, str]:
    """Extract normalized package name -> pinned version from a requirements file."""
    versions: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s]+)", line)
        if match:
            versions[_normalize(match.group(1))] = match.group(2)
    return versions


def test_runtime_dep_versions_match_between_files():
    """For every package present in both requirements.txt and requirements-dev.txt the pinned version must match."""
    requirements_path = PROJECT_ROOT / "requirements.txt"
    requirements_dev_path = PROJECT_ROOT / "requirements-dev.txt"

    assert requirements_path.exists(), "requirements.txt not found"
    assert requirements_dev_path.exists(), "requirements-dev.txt not found"

    req_versions = _parse_requirements_versions(requirements_path)
    dev_versions = _parse_requirements_versions(requirements_dev_path)

    mismatches = []
    for pkg in sorted(req_versions):
        if pkg in dev_versions and req_versions[pkg] != dev_versions[pkg]:
            mismatches.append(
                f"{pkg}: requirements.txt={req_versions[pkg]}, requirements-dev.txt={dev_versions[pkg]}"
            )

    assert not mismatches, (
        "The following packages have different pinned versions between "
        "requirements.txt and requirements-dev.txt:\n" + "\n".join(mismatches)
    )
