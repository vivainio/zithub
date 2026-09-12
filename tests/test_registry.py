"""Unit tests for the local checkout registry (no subprocess boundary
involved — pure filesystem/JSONL, isolated to a temp XDG_DATA_HOME)."""

from __future__ import annotations

import json

import pytest

from zithub import registry


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def test_empty_registry_lists_nothing():
    assert registry.list_seen() == []
    assert registry.find_seen("anything") == []


def test_record_and_list_seen(tmp_path):
    repo_dir = tmp_path / "zithub"
    repo_dir.mkdir()

    registry.record_seen(str(repo_dir), "vivainio/zithub", "main")

    [entry] = registry.list_seen()
    assert entry.path == str(repo_dir)
    assert entry.repo == "vivainio/zithub"
    assert entry.branch == "main"


def test_record_seen_upserts_by_path(tmp_path):
    repo_dir = tmp_path / "zithub"
    repo_dir.mkdir()

    registry.record_seen(str(repo_dir), "vivainio/zithub", "main")
    registry.record_seen(str(repo_dir), "vivainio/zithub", "feature-branch")

    entries = registry.list_seen()
    assert len(entries) == 1
    assert entries[0].branch == "feature-branch"


def test_multiple_checkouts_of_same_repo_kept_separate(tmp_path):
    """Two worktrees/clones of the same repo, at different paths, each get
    their own entry with their own branch -- keyed by path, not by repo."""
    main_checkout = tmp_path / "zithub"
    worktree = tmp_path / "zithub-feature"
    main_checkout.mkdir()
    worktree.mkdir()

    registry.record_seen(str(main_checkout), "vivainio/zithub", "main")
    registry.record_seen(str(worktree), "vivainio/zithub", "feature-branch")

    entries = {e.path: e for e in registry.list_seen()}
    assert len(entries) == 2
    assert entries[str(main_checkout)].branch == "main"
    assert entries[str(worktree)].branch == "feature-branch"


def test_list_seen_orders_most_recent_first(tmp_path, monkeypatch):
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()

    times = iter([100.0, 200.0])
    monkeypatch.setattr(registry.time, "time", lambda: next(times))

    registry.record_seen(str(older), "vivainio/older", "main")
    registry.record_seen(str(newer), "vivainio/newer", "main")

    entries = registry.list_seen()
    assert [e.repo for e in entries] == ["vivainio/newer", "vivainio/older"]


def test_list_seen_drops_paths_that_no_longer_exist(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    registry.record_seen(str(gone), "vivainio/gone", "main")

    gone.rmdir()

    assert registry.list_seen() == []


def test_record_seen_appends_a_line_without_rewriting(tmp_path):
    """Below the compaction threshold, repeated writes to the same path each
    append a new line rather than rewriting the log in place — the whole
    point of the JSONL format (O(1) writes)."""
    repo_dir = tmp_path / "zithub"
    repo_dir.mkdir()

    registry.record_seen(str(repo_dir), "vivainio/zithub", "main")
    registry.record_seen(str(repo_dir), "vivainio/zithub", "feature-branch")

    with open(registry._registry_path(), encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_compaction_dedupes_and_drops_stale_paths(tmp_path, monkeypatch):
    """Once the log exceeds the size threshold, it's rewritten down to one
    line per still-existing path — stale paths dropped, duplicates
    collapsed to their latest record."""
    monkeypatch.setattr(registry, "_COMPACT_SIZE_BYTES", 0)
    gone = tmp_path / "gone"
    kept = tmp_path / "kept"
    gone.mkdir()
    kept.mkdir()

    registry.record_seen(str(gone), "vivainio/gone", "main")
    gone.rmdir()
    registry.record_seen(str(kept), "vivainio/kept", "main")
    registry.record_seen(str(kept), "vivainio/kept", "feature-branch")

    with open(registry._registry_path(), encoding="utf-8") as f:
        lines = [line for line in f.readlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["path"] == str(kept)
    assert json.loads(lines[0])["branch"] == "feature-branch"


def test_find_seen_matches_repo_name_case_insensitively(tmp_path):
    repo_dir = tmp_path / "zithub"
    repo_dir.mkdir()
    registry.record_seen(str(repo_dir), "vivainio/zithub", "main")

    assert len(registry.find_seen("ZITHUB")) == 1
    assert registry.find_seen("nonexistent") == []


def test_find_seen_matches_path_substring(tmp_path):
    repo_dir = tmp_path / "special-checkout"
    repo_dir.mkdir()
    registry.record_seen(str(repo_dir), "vivainio/other-name", "main")

    assert len(registry.find_seen("special-checkout")) == 1
