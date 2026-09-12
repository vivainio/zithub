from __future__ import annotations

import pytest

from zithub import versioning


@pytest.mark.parametrize(
    "tag,part,expected",
    [
        ("v1.2.3", "patch", "v1.2.4"),
        ("v1.2.3", "minor", "v1.3.0"),
        ("v1.2.3", "major", "v2.0.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("v0.9.9", "patch", "v0.9.10"),
        ("V1.0.0", "patch", "V1.0.1"),
    ],
)
def test_bump(tag, part, expected):
    assert versioning.bump(tag, part) == expected


def test_bump_rejects_non_semver_tag():
    with pytest.raises(versioning.VersionError, match="doesn't look like a plain semver tag"):
        versioning.bump("release-2026-01", "patch")


def test_bump_rejects_prerelease_suffix():
    with pytest.raises(versioning.VersionError):
        versioning.bump("v1.2.3-rc1", "patch")


def test_bump_rejects_unknown_part():
    with pytest.raises(ValueError, match="unknown part"):
        versioning.bump("v1.2.3", "epoch")
