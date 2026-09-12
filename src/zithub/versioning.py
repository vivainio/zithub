"""Semver tag bumping — pure logic, no subprocess calls."""

from __future__ import annotations

import re

_SEMVER_RE = re.compile(r"^(?P<prefix>[vV])?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


class VersionError(Exception):
    """Raised when a tag doesn't parse as a plain vX.Y.Z / X.Y.Z semver tag."""


def bump(tag: str, part: str) -> str:
    """`tag` with its major/minor/patch component incremented and the
    components after it reset to 0, preserving a leading "v" if present.
    `part` is one of "major", "minor", "patch"."""
    match = _SEMVER_RE.match(tag)
    if not match:
        raise VersionError(
            f"'{tag}' doesn't look like a plain semver tag (vX.Y.Z) — pass the next version explicitly"
        )
    major, minor, patch = int(match["major"]), int(match["minor"]), int(match["patch"])
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"unknown part: {part!r}")
    return f"{match['prefix'] or ''}{major}.{minor}.{patch}"
