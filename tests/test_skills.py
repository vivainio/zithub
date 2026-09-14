"""Tests for `zh install-skills` and the staleness-nudge it pairs with."""

from __future__ import annotations

import argparse
from datetime import date

import pytest

from zithub import skills


def test_install_skills_copies_skill_md(tmp_path):
    args = argparse.Namespace(skills_dir=str(tmp_path))
    exit_code = skills.install_skills_command(args)

    assert exit_code == 0
    assert (tmp_path / "zithub" / "SKILL.md").exists()


def test_install_skills_creates_target_dir(tmp_path):
    target = tmp_path / "nested" / "skills"
    args = argparse.Namespace(skills_dir=str(target))

    skills.install_skills_command(args)

    assert (target / "zithub" / "SKILL.md").exists()


def test_install_skills_skill_md_has_name(tmp_path):
    args = argparse.Namespace(skills_dir=str(tmp_path))
    skills.install_skills_command(args)

    content = (tmp_path / "zithub" / "SKILL.md").read_text()
    assert "name: zithub" in content


def test_install_skills_defaults_to_claude_skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    args = argparse.Namespace(skills_dir=None)

    skills.install_skills_command(args)

    assert (tmp_path / ".claude" / "skills" / "zithub" / "SKILL.md").exists()


def test_install_skills_honors_claude_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "custom-claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    args = argparse.Namespace(skills_dir=None)

    skills.install_skills_command(args)

    assert (config_dir / "skills" / "zithub" / "SKILL.md").exists()


def test_skills_dir_flag_overrides_claude_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ignored"))
    target = tmp_path / "explicit"
    args = argparse.Namespace(skills_dir=str(target))

    skills.install_skills_command(args)

    assert (target / "zithub" / "SKILL.md").exists()


class TestFrontmatterUpdated:
    def test_extracts_updated_field(self):
        text = "---\nupdated: 2026-07-16\n---\n\nbody"
        assert skills._frontmatter_updated(text) == "2026-07-16"

    def test_returns_none_without_frontmatter(self):
        assert skills._frontmatter_updated("no frontmatter here") is None

    def test_returns_none_without_closing_delimiter(self):
        assert skills._frontmatter_updated("---\nupdated: 2026-07-16\nno closing") is None

    def test_returns_none_when_field_missing(self):
        text = "---\nname: zithub\n---\n\nbody"
        assert skills._frontmatter_updated(text) is None


class TestCheckSkillStaleness:
    def _setup(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        bundled_updated: str | None,
        installed_updated: str | None,
        installed_exists: bool = True,
    ):
        bundled_root = tmp_path / "bundled"
        (bundled_root / "skills" / "zithub").mkdir(parents=True)
        bundled_front_matter = f"updated: {bundled_updated}\n" if bundled_updated else ""
        (bundled_root / "skills" / "zithub" / "SKILL.md").write_text(
            f"---\n{bundled_front_matter}---\n\nBundled body"
        )
        monkeypatch.setattr(skills, "files", lambda package: bundled_root)

        installed_md = tmp_path / "installed" / "SKILL.md"
        if installed_exists:
            installed_md.parent.mkdir(parents=True)
            installed_front_matter = f"updated: {installed_updated}\n" if installed_updated else ""
            installed_md.write_text(f"---\n{installed_front_matter}---\n\nInstalled body")
        monkeypatch.setattr(skills, "_installed_skill_md", lambda: str(installed_md))

        marker = tmp_path / "skill-check.txt"
        monkeypatch.setattr(skills, "_stale_check_marker", lambda: str(marker))
        return marker

    def test_prints_nudge_when_installed_is_older(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, bundled_updated="2026-07-16", installed_updated="2026-07-03")

        skills.check_skill_staleness()

        err = capsys.readouterr().err
        assert "note: installed zithub skill is outdated" in err
        assert "installed: 2026-07-03" in err
        assert "latest: 2026-07-16" in err
        assert "zh install-skills" in err

    def test_silent_when_up_to_date(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, bundled_updated="2026-07-16", installed_updated="2026-07-16")

        skills.check_skill_staleness()

        assert capsys.readouterr().err == ""

    def test_silent_when_no_installed_skill(self, tmp_path, monkeypatch, capsys):
        self._setup(
            tmp_path, monkeypatch, bundled_updated="2026-07-16", installed_updated=None,
            installed_exists=False,
        )

        skills.check_skill_staleness()

        assert capsys.readouterr().err == ""

    def test_throttled_to_once_per_day(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, bundled_updated="2026-07-16", installed_updated="2026-07-03")

        skills.check_skill_staleness()
        assert "outdated" in capsys.readouterr().err

        skills.check_skill_staleness()
        assert capsys.readouterr().err == ""

    def test_writes_marker_with_todays_date(self, tmp_path, monkeypatch):
        marker = self._setup(
            tmp_path, monkeypatch, bundled_updated="2026-07-16", installed_updated="2026-07-16"
        )

        skills.check_skill_staleness()

        assert marker.read_text().strip() == date.today().isoformat()
