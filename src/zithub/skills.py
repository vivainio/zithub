"""Install the bundled Claude Code skill (see skills/zithub/SKILL.md) and
nudge when an already-installed copy has fallen behind it."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from importlib.resources import files

_FRONTMATTER_UPDATED_RE = re.compile(r"(?m)^updated:\s*(\S+)\s*$")


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "zithub")


def _stale_check_marker() -> str:
    return os.path.join(_cache_dir(), "skill-check.txt")


def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _default_skills_dir() -> str:
    return os.path.join(_claude_config_dir(), "skills")


def _installed_skill_md() -> str:
    return os.path.join(_default_skills_dir(), "zithub", "SKILL.md")


def install_skills_command(args: argparse.Namespace) -> int:
    skills_dir = args.skills_dir or _default_skills_dir()
    src = files("zithub") / "skills" / "zithub"
    dest = os.path.join(skills_dir, "zithub")
    os.makedirs(dest, exist_ok=True)
    for item in src.iterdir():
        dest_file = os.path.join(dest, item.name)
        with item.open("rb") as f:
            content = f.read()
        with open(dest_file, "wb") as out:
            out.write(content)
        print(f"installed {dest_file}")
    print(f"skill installed to {dest}")
    return 0


def _frontmatter_updated(text: str) -> str | None:
    """Extract the `updated:` frontmatter field from a skill markdown
    file's YAML frontmatter, if present. A plain regex rather than a YAML
    parser — the one field we need is a bare scalar, and zithub otherwise
    has no dependencies to pull in a YAML library for."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    match = _FRONTMATTER_UPDATED_RE.search(text[4:end])
    return match.group(1) if match else None


def check_skill_staleness() -> None:
    """Print a one-line stderr nudge if the installed skill is older than
    the bundled one. Throttled to once per day via a cache marker, since
    this runs on every CLI invocation."""
    today = date.today().isoformat()
    marker = _stale_check_marker()
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == today:
                return
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(today)

    installed_path = _installed_skill_md()
    if not os.path.exists(installed_path):
        return

    with open(installed_path, encoding="utf-8") as f:
        installed_updated = _frontmatter_updated(f.read())

    bundled = (files("zithub") / "skills" / "zithub" / "SKILL.md").read_text(encoding="utf-8")
    bundled_updated = _frontmatter_updated(bundled)

    if bundled_updated and (not installed_updated or installed_updated < bundled_updated):
        print(
            f"note: installed zithub skill is outdated (installed: {installed_updated or 'unknown'}, "
            f"latest: {bundled_updated}) — run `zh install-skills` to update",
            file=sys.stderr,
        )
