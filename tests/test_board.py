"""Unit tests for the board sqlite cache (no subprocess boundary; isolated
from the real db via the autouse `_isolate_registry` fixture in
conftest.py, which points XDG_DATA_HOME at a temp dir)."""

from __future__ import annotations

import dataclasses

import pytest

from zithub import board, gh

H = "github.com"


def _pr(number, **overrides):
    base = gh.BoardPr(
        repo="acme/widgets",
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/acme/widgets/pull/{number}",
        is_draft=False,
        review_decision="",
        ci_state="success",
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
        comment_count=0,
        last_comment_at=None,
        last_comment_author=None,
    )
    return dataclasses.replace(base, **overrides)


def test_list_board_empty():
    assert board.list_board(H) == []


def test_get_login_defaults_to_none():
    assert board.get_login(H) is None


def test_sync_stores_login_for_later_syncs():
    board.sync(H, [_pr(1)], synced_at="t1", login="vivainio")
    assert board.get_login(H) == "vivainio"


def test_sync_then_list_sorts_oldest_activity_first():
    old = _pr(1, updated_at="2026-01-01T00:00:00Z")
    recent = _pr(2, updated_at="2026-09-01T00:00:00Z")
    board.sync(H, [recent, old], synced_at="2026-09-22T00:00:00Z")

    rows = board.list_board(H)
    assert [r.number for r in rows] == [1, 2]


def test_sync_uses_last_comment_over_updated_at_for_activity():
    pr = _pr(1, updated_at="2026-01-01T00:00:00Z", last_comment_at="2026-09-01T00:00:00Z")
    board.sync(H, [pr], synced_at="2026-09-22T00:00:00Z")

    [row] = board.list_board(H)
    assert row.last_activity_at == "2026-09-01T00:00:00Z"


def test_sync_replaces_previous_contents():
    board.sync(H, [_pr(1)], synced_at="t1")
    board.sync(H, [_pr(2)], synced_at="t2")

    rows = board.list_board(H)
    assert [r.number for r in rows] == [2]


def test_run_query_selects_synced_data():
    board.sync(H, [_pr(1, ci_state="failed")], synced_at="t1")

    columns, rows = board.run_query(H, "select number, ci_state from prs")
    assert columns == ["number", "ci_state"]
    assert list(rows[0]) == [1, "failed"]


@pytest.mark.parametrize("sql", ["delete from prs", "update prs set title='x'", "drop table prs"])
def test_run_query_rejects_non_select(sql):
    with pytest.raises(board.BoardError):
        board.run_query(H, sql)


def test_boards_are_separate_per_host():
    board.sync("github.com", [_pr(1)], synced_at="t1", login="vivainio")
    board.sync("ghe.example.com", [_pr(2)], synced_at="t1", login="vvainio")

    assert [r.number for r in board.list_board("github.com")] == [1]
    assert [r.number for r in board.list_board("ghe.example.com")] == [2]
    assert board.get_login("ghe.example.com") == "vvainio"


def test_prune_and_upsert_keep_untouched_rows():
    board.sync(H, [_pr(1), _pr(2), _pr(3)], synced_at="t1")
    board.prune(H, {("acme/widgets", 1), ("acme/widgets", 2)})
    board.upsert(H, [_pr(2, ci_state="failed")], synced_at="t2")

    assert board.cached_versions(H) == {
        ("acme/widgets", 1): ("2026-09-01T00:00:00Z", "success"),
        ("acme/widgets", 2): ("2026-09-01T00:00:00Z", "failed"),
    }


def test_rows_cached_under_an_older_cache_version_are_dropped():
    board.sync(H, [_pr(1)], synced_at="t1")
    with board._connect(H) as conn:
        conn.execute("PRAGMA user_version = 0")
    assert board.list_board(H) == []
    board.sync(H, [_pr(1)], synced_at="t2")
    assert [r.number for r in board.list_board(H)] == [1]
