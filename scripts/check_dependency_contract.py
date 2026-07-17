#!/usr/bin/env python3
"""Compile and statically validate the repository Python dependency contract.

Static validation covers active direct requirements, exact pins, markers, shared
pin parity, the hashed container closure, generated headers, and documented
entrypoints. Clean source-only resolution is established separately by
``--verify-bootstrap``; install closure is proved by installing generated locks
and running ``pip check``.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

if __package__:
    from .dependency_lock_parser import (
        PYTHON_LOCK_VERSION,
        UNSAFE_OMISSION_FOOTER as _UNSAFE_OMISSION_FOOTER,
        ParsedLock,
        is_active as _is_active,
        parse_exact_pin as _parse_exact_pin,
        parse_generated_lock,
    )
else:
    from dependency_lock_parser import (
        PYTHON_LOCK_VERSION,
        UNSAFE_OMISSION_FOOTER as _UNSAFE_OMISSION_FOOTER,
        ParsedLock,
        is_active as _is_active,
        parse_exact_pin as _parse_exact_pin,
        parse_generated_lock,
    )

UNSAFE_OMISSION_FOOTER = _UNSAFE_OMISSION_FOOTER

SECURITY_CONSTRAINTS_FILE = "security-constraints.txt"
COMPATIBILITY_CONSTRAINTS_FILE = "compatibility-constraints.txt"
LOCK_COMMANDS = {
    "requirements.txt": (
        f"pip-compile --allow-unsafe --constraint={SECURITY_CONSTRAINTS_FILE} "
        f"--constraint={COMPATIBILITY_CONSTRAINTS_FILE} "
        "--output-file=requirements.txt --strip-extras pyproject.toml"
    ),
    "requirements-dev.txt": (
        f"pip-compile --allow-unsafe --constraint={SECURITY_CONSTRAINTS_FILE} "
        f"--constraint={COMPATIBILITY_CONSTRAINTS_FILE} "
        "--constraint=requirements.txt --extra=dev "
        "--output-file=requirements-dev.txt --strip-extras pyproject.toml"
    ),
    "requirements-research.txt": (
        f"pip-compile --allow-unsafe --constraint={SECURITY_CONSTRAINTS_FILE} "
        f"--constraint={COMPATIBILITY_CONSTRAINTS_FILE} "
        "--constraint=requirements-dev.txt --constraint=requirements.txt "
        "--extra=research --output-file=requirements-research.txt --strip-extras pyproject.toml"
    ),
    "requirements-container.txt": (
        f"pip-compile --allow-unsafe --constraint={SECURITY_CONSTRAINTS_FILE} "
        f"--constraint={COMPATIBILITY_CONSTRAINTS_FILE} "
        "--constraint=requirements-research.txt --extra=research --generate-hashes "
        "--output-file=requirements-container.txt --strip-extras pyproject.toml"
    ),
}
README_FILES = ("README.md", "README_EN.MD")
BOOTSTRAP_SOURCE_FILES = (
    "pyproject.toml",
    "llm_gateway_core/__init__.py",
    "llm_gateway_core/version.py",
    SECURITY_CONSTRAINTS_FILE,
    COMPATIBILITY_CONSTRAINTS_FILE,
    *README_FILES,
)
COMPILE_SCRIPT_COMMAND = "./scripts/compile_requirements.sh"
REQUIRED_DEV_DEPENDENCIES = frozenset(
    {"httpx2", "pip-audit", "pip-tools", "pytest-cov", "pytest-playwright"}
)
REQUIRED_BUILD_SYSTEM_DEPENDENCIES = frozenset({"setuptools", "wheel"})
REQUIRED_SECURITY_CONSTRAINTS = frozenset(
    {
        "aiohttp",
        "cloakbrowser",
        "cryptography",
        "fastapi",
        "idna",
        "langchain",
        "langsmith",
        "mistune",
        "nltk",
        "pydantic-settings",
        "pypdf",
        "python-multipart",
        "starlette",
        "weasyprint",
    }
)
REQUIRED_COMPATIBILITY_CONSTRAINTS = {"requests": "<2.34"}
RESEARCH_ONLY_DEPENDENCIES = frozenset({"gpt-researcher"})
REQUIRED_CONTAINER_DEPENDENCIES = frozenset(
    {"cloakbrowser", "gpt-researcher", "playwright"}
)
FORBIDDEN_CONTAINER_DEV_DEPENDENCIES = frozenset(
    {
        "httpx2",
        "pip-audit",
        "pip-tools",
        "pytest",
        "pytest-cov",
        "pytest-playwright",
        "ruff",
    }
)
REQUIRED_PYTEST_MARKERS = frozenset({"browser"})
REQUIRED_UNSAFE_PINS = {
    "requirements.txt": frozenset({"setuptools"}),
    "requirements-dev.txt": frozenset({"pip", "setuptools"}),
    "requirements-research.txt": frozenset({"setuptools"}),
    "requirements-container.txt": frozenset({"setuptools"}),
}
UPSTREAM_WARNING_POLICY_ID = "gpt-researcher-langchain-community"
UPSTREAM_WARNING_POLICY_PACKAGE = "gpt-researcher"
UPSTREAM_WARNING_MESSAGE = (
    "`langchain-community` is being sunset and is no longer actively maintained. "
    "See https://github.com/langchain-ai/langchain-community/issues/674 for details "
    "and migration guidance toward standalone integration packages."
)
UPSTREAM_WARNING_MESSAGE_PATTERN = (
    r"^`langchain-community` is being sunset and is no longer actively maintained\. "
    r"See https\x3a//github\.com/langchain-ai/langchain-community/issues/674 for details "
    r"and migration guidance toward standalone integration packages\.$"
)
UPSTREAM_WARNING_CATEGORY = "DeprecationWarning"
UPSTREAM_WARNING_MODULE_PATTERN = r"^gpt_researcher\.scraper\.arxiv\.arxiv$"
UPSTREAM_WARNING_POLICY_FILTER = (
    f"ignore:{UPSTREAM_WARNING_MESSAGE_PATTERN}"
    f":{UPSTREAM_WARNING_CATEGORY}:{UPSTREAM_WARNING_MODULE_PATTERN}"
)
@dataclass(frozen=True)
class ParsedConstraints:
    filename: str
    requirements: dict[str, Requirement]


class DependencyContractError(RuntimeError):
    """Raised when the static repository dependency contract is inconsistent."""


class DependencyCompilationError(RuntimeError):
    """Raised when canonical lock compilation cannot complete."""


def _load_pyproject(root: Path, errors: list[str]) -> dict:
    path = root / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"Cannot read {path.name}: {exc}")
        return {}


def _parse_declared_requirements(
    values: object,
    *,
    section: str,
    errors: list[str],
) -> dict[str, Requirement]:
    if not isinstance(values, list):
        errors.append(f"{section} must be an array of PEP 508 requirement strings")
        return {}

    parsed: dict[str, Requirement] = {}
    for value in values:
        if not isinstance(value, str):
            errors.append(f"{section} contains a non-string dependency: {value!r}")
            continue
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            errors.append(f"{section} contains invalid requirement {value!r}: {exc}")
            continue
        if not _is_active(requirement):
            continue
        name = canonicalize_name(requirement.name)
        if name in parsed:
            errors.append(f"{section} declares {name!r} more than once for Python {PYTHON_LOCK_VERSION}")
            continue
        parsed[name] = requirement
    return parsed


def _parse_lock(root: Path, filename: str, errors: list[str]) -> ParsedLock:
    return parse_generated_lock(
        root,
        filename,
        errors,
        compile_script_command=COMPILE_SCRIPT_COMMAND,
    )


def _parse_security_constraints(root: Path, errors: list[str]) -> ParsedLock:
    path = root / SECURITY_CONSTRAINTS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"Cannot read {SECURITY_CONSTRAINTS_FILE}: {exc}")
        return ParsedLock(filename=SECURITY_CONSTRAINTS_FILE, pins={}, hashes={})

    pins: dict[str, Version] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            errors.append(
                f"{SECURITY_CONSTRAINTS_FILE}:{line_number} must contain an exact pin, not an option: {line}"
            )
            continue
        parsed_pin = _parse_exact_pin(
            line,
            filename=SECURITY_CONSTRAINTS_FILE,
            line_number=line_number,
            errors=errors,
        )
        if parsed_pin is None:
            continue
        requirement, version = parsed_pin
        if not _is_active(requirement):
            continue

        name = canonicalize_name(requirement.name)
        if name in pins:
            errors.append(f"{SECURITY_CONSTRAINTS_FILE} pins active requirement {name!r} more than once")
            continue
        pins[name] = version

    missing = REQUIRED_SECURITY_CONSTRAINTS - pins.keys()
    if missing:
        errors.append(
            f"{SECURITY_CONSTRAINTS_FILE} is missing required security pins: "
            + ", ".join(sorted(missing))
        )
    return ParsedLock(filename=SECURITY_CONSTRAINTS_FILE, pins=pins, hashes={})


def _parse_compatibility_constraints(root: Path, errors: list[str]) -> ParsedConstraints:
    path = root / COMPATIBILITY_CONSTRAINTS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"Cannot read {COMPATIBILITY_CONSTRAINTS_FILE}: {exc}")
        return ParsedConstraints(filename=COMPATIBILITY_CONSTRAINTS_FILE, requirements={})

    requirements: dict[str, Requirement] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            errors.append(
                f"{COMPATIBILITY_CONSTRAINTS_FILE}:{line_number} must contain a requirement, "
                f"not an option: {line}"
            )
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            errors.append(
                f"{COMPATIBILITY_CONSTRAINTS_FILE}:{line_number} is not a valid requirement: {exc}"
            )
            continue
        if requirement.url is not None or not requirement.specifier:
            errors.append(
                f"{COMPATIBILITY_CONSTRAINTS_FILE}:{line_number} must contain a version constraint: {line}"
            )
            continue
        if not _is_active(requirement):
            continue

        name = canonicalize_name(requirement.name)
        if name in requirements:
            errors.append(
                f"{COMPATIBILITY_CONSTRAINTS_FILE} constrains active requirement {name!r} more than once"
            )
            continue
        requirements[name] = requirement

    for name, expected_specifier in REQUIRED_COMPATIBILITY_CONSTRAINTS.items():
        requirement = requirements.get(name)
        if requirement is None:
            errors.append(
                f"{COMPATIBILITY_CONSTRAINTS_FILE} is missing required compatibility constraint: "
                f"{name}{expected_specifier}"
            )
        elif (
            requirement.extras
            or requirement.marker is not None
            or str(requirement.specifier) != expected_specifier
        ):
            errors.append(
                f"{COMPATIBILITY_CONSTRAINTS_FILE} must declare the reviewed compatibility boundary "
                f"{name}{expected_specifier}, found {requirement}"
            )

    return ParsedConstraints(
        filename=COMPATIBILITY_CONSTRAINTS_FILE,
        requirements=requirements,
    )


def _validate_direct_requirements(
    lock: ParsedLock,
    declared_groups: tuple[dict[str, Requirement], ...],
    errors: list[str],
) -> None:
    for group in declared_groups:
        for name, requirement in sorted(group.items()):
            pinned = lock.pins.get(name)
            if pinned is None:
                errors.append(
                    f"{lock.filename} is missing an active pin for direct dependency {name!r}"
                )
                continue
            if requirement.specifier and not requirement.specifier.contains(pinned, prereleases=True):
                errors.append(
                    f"{lock.filename} pins {name}=={pinned}, which does not satisfy "
                    f"{requirement.specifier} from pyproject.toml"
                )


def _validate_build_system(
    pyproject: dict,
    dev_lock: ParsedLock,
    errors: list[str],
) -> None:
    build_system = pyproject.get("build-system", {}) if isinstance(pyproject, dict) else {}
    if not isinstance(build_system, dict):
        errors.append("[build-system] must be a table")
        return
    if build_system.get("build-backend") != "setuptools.build_meta":
        errors.append("[build-system].build-backend must be 'setuptools.build_meta'")

    values = build_system.get("requires", [])
    if not isinstance(values, list):
        errors.append("[build-system].requires must be an array")
        return

    pinned: dict[str, Version] = {}
    for value in values:
        if not isinstance(value, str):
            errors.append(f"[build-system].requires contains a non-string dependency: {value!r}")
            continue
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            errors.append(f"[build-system].requires contains invalid requirement {value!r}: {exc}")
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            errors.append(
                f"[build-system].requires must exactly pin build dependency {requirement.name!r}: {value}"
            )
            continue
        name = canonicalize_name(requirement.name)
        try:
            version = Version(specifiers[0].version)
        except InvalidVersion as exc:
            errors.append(f"[build-system].requires has invalid version for {name}: {exc}")
            continue
        if name in pinned:
            errors.append(f"[build-system].requires declares {name!r} more than once")
            continue
        pinned[name] = version

    missing = REQUIRED_BUILD_SYSTEM_DEPENDENCIES - pinned.keys()
    if missing:
        errors.append(
            "Missing required exact build-system dependencies: "
            + ", ".join(sorted(missing))
        )
    for name, version in sorted(pinned.items()):
        locked_version = dev_lock.pins.get(name)
        if locked_version is None:
            errors.append(
                f"requirements-dev.txt is missing build-system dependency {name}=={version}"
            )
        elif locked_version != version:
            errors.append(
                f"Build-system pin mismatch for {name}: "
                f"pyproject.toml={version}, requirements-dev.txt={locked_version}"
            )


def _validate_shared_pins(
    left: ParsedLock,
    right: ParsedLock,
    *,
    require_all_left: bool,
    errors: list[str],
) -> None:
    for name, left_version in sorted(left.pins.items()):
        right_version = right.pins.get(name)
        if right_version is None:
            if require_all_left:
                errors.append(
                    f"{right.filename} is missing active pin {name!r} from {left.filename}"
                )
            continue
        if right_version != left_version:
            errors.append(
                f"Shared pin mismatch for {name}: {left.filename}={left_version}, "
                f"{right.filename}={right_version}"
            )


def _validate_container_lock(
    research_lock: ParsedLock,
    container_lock: ParsedLock,
    errors: list[str],
) -> None:
    missing = research_lock.pins.keys() - container_lock.pins.keys()
    if missing:
        errors.append(
            "requirements-container.txt is missing research production pins: "
            + ", ".join(sorted(missing))
        )

    extra = container_lock.pins.keys() - research_lock.pins.keys()
    if extra:
        errors.append(
            "requirements-container.txt contains pins outside the research production closure: "
            + ", ".join(sorted(extra))
        )

    for name in sorted(research_lock.pins.keys() & container_lock.pins.keys()):
        research_version = research_lock.pins[name]
        container_version = container_lock.pins[name]
        if research_version != container_version:
            errors.append(
                f"Container pin mismatch for {name}: "
                f"requirements-research.txt={research_version}, "
                f"requirements-container.txt={container_version}"
            )

    missing_required = REQUIRED_CONTAINER_DEPENDENCIES - container_lock.pins.keys()
    if missing_required:
        errors.append(
            "requirements-container.txt is missing required production dependencies: "
            + ", ".join(sorted(missing_required))
        )

    forbidden = FORBIDDEN_CONTAINER_DEV_DEPENDENCIES & container_lock.pins.keys()
    if forbidden:
        errors.append(
            "requirements-container.txt contains development-only dependencies: "
            + ", ".join(sorted(forbidden))
        )

    unhashed = {
        name for name in container_lock.pins if not container_lock.hashes.get(name)
    }
    if unhashed:
        errors.append(
            "requirements-container.txt has active pins without SHA-256 hashes: "
            + ", ".join(sorted(unhashed))
        )


def _validate_security_constraints(
    constraints: ParsedLock,
    locks: tuple[ParsedLock, ...],
    errors: list[str],
) -> None:
    for name, constrained_version in sorted(constraints.pins.items()):
        matching_locks = [lock for lock in locks if name in lock.pins]
        if not matching_locks:
            errors.append(
                f"Security constraint {name}=={constrained_version} is not present in any generated lock"
            )
            continue
        for lock in matching_locks:
            locked_version = lock.pins[name]
            if locked_version != constrained_version:
                errors.append(
                    f"Security constraint mismatch for {name}: "
                    f"{constraints.filename}={constrained_version}, "
                    f"{lock.filename}={locked_version}"
                )


def _validate_compatibility_constraints(
    constraints: ParsedConstraints,
    locks: tuple[ParsedLock, ...],
    errors: list[str],
) -> None:
    for name, requirement in sorted(constraints.requirements.items()):
        matching_locks = [lock for lock in locks if name in lock.pins]
        if not matching_locks:
            errors.append(
                f"Compatibility constraint {requirement} is not present in any generated lock"
            )
            continue
        for lock in matching_locks:
            locked_version = lock.pins[name]
            if not requirement.specifier.contains(locked_version, prereleases=True):
                errors.append(
                    f"Compatibility constraint mismatch for {name}: "
                    f"{constraints.filename}={requirement.specifier}, "
                    f"{lock.filename}={locked_version}"
                )


def _validate_unsafe_pins(locks: dict[str, ParsedLock], errors: list[str]) -> None:
    for filename, required_names in REQUIRED_UNSAFE_PINS.items():
        missing = required_names - locks[filename].pins.keys()
        if missing:
            errors.append(
                f"{filename} is missing required active unsafe pins: "
                + ", ".join(sorted(missing))
            )


def _validate_pytest_markers(pyproject: dict, errors: list[str]) -> None:
    tool = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
    pytest_config = tool.get("pytest", {}) if isinstance(tool, dict) else {}
    ini_options = pytest_config.get("ini_options", {}) if isinstance(pytest_config, dict) else {}
    marker_entries = ini_options.get("markers", []) if isinstance(ini_options, dict) else []
    if not isinstance(marker_entries, list):
        errors.append("[tool.pytest.ini_options].markers must be an array")
        return
    marker_names = {
        entry.split(":", 1)[0].strip()
        for entry in marker_entries
        if isinstance(entry, str)
    }
    missing = REQUIRED_PYTEST_MARKERS - marker_names
    if missing:
        errors.append("Missing required pytest markers: " + ", ".join(sorted(missing)))


def _validate_upstream_warning_policy(
    pyproject: dict,
    dev_lock: ParsedLock,
    *,
    as_of: date,
    errors: list[str],
) -> None:
    tool = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
    pytest_config = tool.get("pytest", {}) if isinstance(tool, dict) else {}
    ini_options = pytest_config.get("ini_options", {}) if isinstance(pytest_config, dict) else {}
    filterwarnings = ini_options.get("filterwarnings", []) if isinstance(ini_options, dict) else []
    expected_filters = [f"error::{UPSTREAM_WARNING_CATEGORY}", UPSTREAM_WARNING_POLICY_FILTER]
    if filterwarnings != expected_filters:
        errors.append(
            "[tool.pytest.ini_options].filterwarnings must reject all deprecation warnings "
            "and exactly the versioned gpt-researcher upstream filter"
        )

    llmgateway = tool.get("llmgateway", {}) if isinstance(tool, dict) else {}
    policies = (
        llmgateway.get("upstream-warning-policy", [])
        if isinstance(llmgateway, dict)
        else []
    )
    if not isinstance(policies, list) or len(policies) != 1 or not isinstance(policies[0], dict):
        errors.append("[tool.llmgateway.upstream-warning-policy] must define exactly one policy")
        return

    policy = policies[0]
    if policy.get("id") != UPSTREAM_WARNING_POLICY_ID:
        errors.append(f"Upstream warning policy id must be {UPSTREAM_WARNING_POLICY_ID!r}")
    if policy.get("package") != UPSTREAM_WARNING_POLICY_PACKAGE:
        errors.append(
            f"Upstream warning policy package must be {UPSTREAM_WARNING_POLICY_PACKAGE!r}"
        )
    if policy.get("filter") != UPSTREAM_WARNING_POLICY_FILTER:
        errors.append("Upstream warning policy filter differs from the exact pytest filter")
    if not isinstance(policy.get("reason"), str) or not policy["reason"].strip():
        errors.append("Upstream warning policy must document a non-empty reason")

    expires = policy.get("expires")
    if not isinstance(expires, date):
        errors.append("Upstream warning policy expires must be a TOML date")
    elif as_of > expires:
        errors.append(
            f"Upstream warning policy expired on {expires.isoformat()}; remove or re-review it"
        )

    package_version = policy.get("package_version")
    locked_version = dev_lock.pins.get(UPSTREAM_WARNING_POLICY_PACKAGE)
    if not isinstance(package_version, str) or locked_version is None:
        errors.append("Upstream warning policy must reference the locked gpt-researcher version")
    else:
        try:
            policy_version = Version(package_version)
        except InvalidVersion:
            errors.append("Upstream warning policy package_version is invalid")
        else:
            if policy_version != locked_version:
                errors.append(
                    "Upstream warning policy package_version does not match "
                    f"requirements-dev.txt: {policy_version} != {locked_version}"
                )


def _validate_documented_entrypoint(root: Path, errors: list[str]) -> None:
    for readme_filename in README_FILES:
        path = root / readme_filename
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Cannot read {readme_filename}: {exc}")
            continue
        if COMPILE_SCRIPT_COMMAND not in content:
            errors.append(
                f"{readme_filename} must call the canonical dependency entrypoint: "
                f"{COMPILE_SCRIPT_COMMAND}"
            )
        if re.search(
            r"(?m)^\s*(?:>\s*)?(?:\$\s*)?pip-compile(?:\s|$)",
            content,
        ):
            errors.append(
                f"{readme_filename} duplicates pip-compile commands; document only "
                f"{COMPILE_SCRIPT_COMMAND}"
            )


def _validate_compile_commands(errors: list[str]) -> None:
    required_flags = {
        "--allow-unsafe",
        f"--constraint={SECURITY_CONSTRAINTS_FILE}",
        f"--constraint={COMPATIBILITY_CONSTRAINTS_FILE}",
    }
    for filename, command in LOCK_COMMANDS.items():
        command_flags = set(shlex.split(command))
        missing = required_flags - command_flags
        if missing:
            errors.append(
                f"Canonical command for {filename} is missing required flags: "
                + ", ".join(sorted(missing))
            )
        has_hashes = "--generate-hashes" in command_flags
        if filename == "requirements-container.txt" and not has_hashes:
            errors.append(
                "Canonical command for requirements-container.txt is missing --generate-hashes"
            )
        elif filename != "requirements-container.txt" and has_hashes:
            errors.append(
                f"Canonical command for {filename} must not add --generate-hashes"
            )


def collect_dependency_contract_errors(
    root: Path,
    *,
    as_of: date | None = None,
) -> list[str]:
    """Return static dependency-contract violations for the Python 3.12 target.

    This function intentionally does not emulate pip's resolver or prove that
    transitive/extras closure is installable. Source-only bootstrap compilation,
    followed by a clean lock install and ``pip check``, owns that boundary.
    """
    root = root.resolve()
    effective_date = as_of or date.today()
    errors: list[str] = []
    _validate_compile_commands(errors)
    pyproject = _load_pyproject(root, errors)
    project = pyproject.get("project", {}) if isinstance(pyproject, dict) else {}
    optional = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}

    runtime = _parse_declared_requirements(
        project.get("dependencies", []) if isinstance(project, dict) else [],
        section="[project].dependencies",
        errors=errors,
    )
    dev = _parse_declared_requirements(
        optional.get("dev", []) if isinstance(optional, dict) else [],
        section="[project.optional-dependencies].dev",
        errors=errors,
    )
    research = _parse_declared_requirements(
        optional.get("research", []) if isinstance(optional, dict) else [],
        section="[project.optional-dependencies].research",
        errors=errors,
    )

    missing_dev_contract = REQUIRED_DEV_DEPENDENCIES - dev.keys()
    if missing_dev_contract:
        errors.append(
            "Missing required development dependencies from dev extra: "
            + ", ".join(sorted(missing_dev_contract))
        )

    security_constraints = _parse_security_constraints(root, errors)
    compatibility_constraints = _parse_compatibility_constraints(root, errors)
    locks = {
        filename: _parse_lock(root, filename, errors)
        for filename in LOCK_COMMANDS
    }
    runtime_lock = locks["requirements.txt"]
    dev_lock = locks["requirements-dev.txt"]
    research_lock = locks["requirements-research.txt"]
    container_lock = locks["requirements-container.txt"]

    for name in sorted(RESEARCH_ONLY_DEPENDENCIES):
        if name in runtime:
            errors.append(f"Research-only dependency {name!r} must not be declared as runtime")
        if name not in research:
            errors.append(f"Research-only dependency {name!r} is missing from the research extra")
        if name in runtime_lock.pins:
            errors.append(f"Research-only dependency {name!r} must not be pinned in requirements.txt")

    _validate_direct_requirements(runtime_lock, (runtime,), errors)
    _validate_direct_requirements(dev_lock, (runtime, dev), errors)
    _validate_direct_requirements(research_lock, (runtime, research), errors)
    _validate_direct_requirements(container_lock, (runtime, research), errors)
    _validate_build_system(pyproject, dev_lock, errors)
    _validate_shared_pins(runtime_lock, dev_lock, require_all_left=True, errors=errors)
    _validate_shared_pins(runtime_lock, research_lock, require_all_left=True, errors=errors)
    _validate_shared_pins(dev_lock, research_lock, require_all_left=False, errors=errors)
    _validate_container_lock(research_lock, container_lock, errors)
    _validate_security_constraints(
        security_constraints,
        (runtime_lock, dev_lock, research_lock, container_lock),
        errors,
    )
    _validate_compatibility_constraints(
        compatibility_constraints,
        (runtime_lock, dev_lock, research_lock, container_lock),
        errors,
    )
    _validate_unsafe_pins(locks, errors)
    _validate_pytest_markers(pyproject, errors)
    _validate_upstream_warning_policy(
        pyproject,
        dev_lock,
        as_of=effective_date,
        errors=errors,
    )
    _validate_documented_entrypoint(root, errors)
    return errors


def check_dependency_contract(root: Path) -> None:
    """Raise :class:`DependencyContractError` for static contract violations."""
    errors = collect_dependency_contract_errors(root)
    if errors:
        formatted = "\n".join(f"- {error}" for error in errors)
        raise DependencyContractError(f"Dependency contract violations:\n{formatted}")


def compile_requirement_locks(
    root: Path,
    *,
    pip_compile_executable: str | None = None,
) -> None:
    """Fail-fast compile all locks in dependency order, then validate them."""
    executable = pip_compile_executable or shutil.which("pip-compile")
    if executable is None:
        raise DependencyCompilationError(
            "pip-compile is unavailable; install the pinned requirements-dev.txt first"
        )

    for command in LOCK_COMMANDS.values():
        argv = shlex.split(command)
        argv[0] = executable
        try:
            subprocess.run(
                argv,
                cwd=root,
                check=True,
                env={**os.environ, "CUSTOM_COMPILE_COMMAND": COMPILE_SCRIPT_COMMAND},
            )
        except subprocess.CalledProcessError as exc:
            raise DependencyCompilationError(
                f"Lock compilation failed with exit code {exc.returncode}: {command}"
            ) from exc

    check_dependency_contract(root)


def verify_bootstrap_compilation(
    root: Path,
    *,
    pip_compile_executable: str | None = None,
) -> None:
    """Compile from source inputs in an isolated root with no prior output locks."""
    source_root = root.resolve()
    with TemporaryDirectory(prefix="llmgateway-dependency-bootstrap-") as temp_dir:
        bootstrap_root = Path(temp_dir)
        for filename in BOOTSTRAP_SOURCE_FILES:
            source = source_root / filename
            if not source.is_file():
                raise DependencyCompilationError(
                    f"Cannot prepare clean bootstrap: missing source input {filename}"
                )
            try:
                destination = bootstrap_root / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            except OSError as exc:
                raise DependencyCompilationError(
                    f"Cannot prepare clean bootstrap source input {filename}: {exc}"
                ) from exc

        compile_requirement_locks(
            bootstrap_root,
            pip_compile_executable=pip_compile_executable,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the parent of scripts/).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--compile",
        action="store_true",
        help="Compile runtime, dev, research, and container locks before static validation.",
    )
    mode.add_argument(
        "--verify-bootstrap",
        action="store_true",
        help="Compile all locks in a temporary root containing source inputs only.",
    )
    args = parser.parse_args(argv)

    try:
        if args.compile:
            compile_requirement_locks(args.root)
        elif args.verify_bootstrap:
            verify_bootstrap_compilation(args.root)
        else:
            check_dependency_contract(args.root)
    except (DependencyCompilationError, DependencyContractError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.compile:
        print("Dependency locks compiled and contract is consistent.")
    elif args.verify_bootstrap:
        print("Clean bootstrap compilation and contract are consistent.")
    else:
        print("Static dependency contract is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
