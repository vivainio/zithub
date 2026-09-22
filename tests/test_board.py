"""Unit tests for the board sqlite cache (no subprocess boundary; isolated
from the real db via the autouse `_isolate_registry` fixture in
conftest.py, which points XDG_DATA_HOME at a temp dir)."""

from __future__ import annotations

import pytest

from zithub import board, gh


def _pr(number, **overrides):
    fields = dict(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/acme/widgets/pull/{number}",
        state="OPEN",
        is_draft=False,
        updated_at="2026-09-01T00:00:00Z",
        repo="acme/widgets",
        review_decision="",
        ci_state="success",
        created_at="2026-08-01T00:00:00Z",
        comment_count=0,
        last_comment_at=None,
        last_comment_author=None,
    )
    fields.update(overrides)
    return gh.PullRequestSummary(**fields)


def test_list_board_empty():
    assert board.list_board() == []


def test_sync_then_list_sorts_oldest_activity_first():
    old = _pr(1, updated_at="2026-01-01T00:00:00Z")
    recent = _pr(2, updated_at="2026-09-01T00:00:00Z")
    board.sync([recent, old], synced_at="2026-09-22T00:00:00Z")

    rows = board.list_board()
    assert [r.number for r in rows] == [1, 2]


def test_sync_uses_last_comment_over_updated_at_for_activity():
    pr = _pr(1, updated_at="2026-01-01T00:00:00Z", last_comment_at="2026-09-01T00:00:00Z")
    board.sync([pr], synced_at="2026-09-22T00:00:00Z")

    [row] = board.list_board()
    assert row.last_activity_at == "2026-09-01T00:00:00Z"


def test_sync_replaces_previous_contents():
    board.sync([_pr(1)], synced_at="t1")
    board.sync([_pr(2)], synced_at="t2")

    rows = board.list_board()
    assert [r.number for r in rows] == [2]


def test_run_query_selects_synced_data():
    board.sync([_pr(1, ci_state="failed")], synced_at="t1")

    columns, rows = board.run_query("select number, ci_state from prs")
    assert columns == ["number", "ci_state"]
    assert list(rows[0]) == [1, "failed"]


@pytest.mark.parametrize("sql", ["delete from prs", "update prs set title='x'", "drop table prs"])
def test_run_query_rejects_non_select(sql):
    with pytest.raises(board.BoardError):
        board.run_query(sql)
