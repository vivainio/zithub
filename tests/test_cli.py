"""End-to-end: argparse -> cmd_* -> gh.* -> subprocess, via fake_cli."""

from __future__ import annotations

import dataclasses
import json

import pytest

from zithub import gh
from zithub.cli import build_parser

_PR_FIELDS = gh._PR_VIEW_FIELDS


def run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def pr_json(**overrides) -> str:
    data = {
        "number": 7,
        "title": "Add widget",
        "url": "https://github.com/acme/widgets/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "headRefName": "feature",
        "baseRefName": "main",
        "statusCheckRollup": [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u1"}
        ],
    }
    data.update(overrides)
    return json.dumps(data)


def repo_json() -> str:
    return json.dumps(
        {
            "owner": {"login": "acme"},
            "name": "widgets",
            "nameWithOwner": "acme/widgets",
            "url": "https://github.com/acme/widgets",
            "defaultBranchRef": {"name": "main"},
        }
    )


def empty_threads_json() -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# review threads

def test_threads_lists_unresolved_by_default(fake_cli, capsys):
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T_1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "id": "C_1",
                                                    "body": "fix this",
                                                    "createdAt": "2026-01-01T00:00:00Z",
                                                    "path": "a.py",
                                                    "line": 3,
                                                    "author": {"login": "alice"},
                                                }
                                            ]
                                        },
                                    },
                                    {"id": "T_2", "isResolved": True, "comments": {"nodes": []}},
                                ],
                            }
                        }
                    }
                }
            }
        ),
    )
    rc = run(["pr", "threads"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "T_1" in out
    assert "T_2" not in out


def test_reply_and_resolve(fake_cli, capsys):
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body}) { comment { url } } }",
            "-F",
            "id=T_1",
            "-f",
            "body=done",
        ],
        stdout=json.dumps(
            {"data": {"addPullRequestReviewThreadReply": {"comment": {"url": "https://x/comment"}}}}
        ),
    )
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
            "-F",
            "id=T_1",
        ],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
    )
    rc = run(["pr", "reply", "T_1", "-b", "done", "--resolve"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "replied" in out
    assert "resolved" in out


def test_resolve_explicit_ids_skips_repo_lookup(fake_cli):
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
            "-F",
            "id=T_1",
        ],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
    )
    assert run(["pr", "resolve", "T_1"]) == 0
    assert fake_cli.calls == [
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
            "-F",
            "id=T_1",
        ]
    ]


def test_resolve_all_no_threads(fake_cli, capsys):
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=empty_threads_json(),
    )
    rc = run(["pr", "resolve", "--all"])
    assert rc == 1
    assert "no threads to resolve" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# merge preflight

def test_merge_blocks_draft(fake_cli, capsys):
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json(isDraft=True))
    rc = run(["pr", "merge"])
    assert rc == 1
    assert "draft" in capsys.readouterr().err


def test_merge_blocks_changes_requested(fake_cli, capsys):
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json(reviewDecision="CHANGES_REQUESTED")
    )
    rc = run(["pr", "merge"])
    assert rc == 1
    assert "changes requested" in capsys.readouterr().err


def test_merge_blocks_unresolved_threads(fake_cli, capsys):
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"id": "T_1", "isResolved": False, "comments": {"nodes": []}}],
                            }
                        }
                    }
                }
            }
        ),
    )
    rc = run(["pr", "merge"])
    assert rc == 1
    assert "unresolved review thread" in capsys.readouterr().err


def test_merge_blocks_failed_checks(fake_cli, capsys):
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(
            statusCheckRollup=[
                {"name": "build", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "u1"}
            ]
        ),
    )
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=empty_threads_json(),
    )
    rc = run(["pr", "merge", "--no-wait"])
    assert rc == 1
    assert "failing check" in capsys.readouterr().err


def test_merge_succeeds_when_clean(fake_cli):
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=empty_threads_json(),
    )
    fake_cli.set(["gh", "pr", "merge", "--squash"], returncode=0)
    assert run(["pr", "merge", "--no-wait"]) == 0


def test_ship_defaults_delete_branch(fake_cli):
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=empty_threads_json(),
    )
    fake_cli.set(["gh", "pr", "merge", "--squash", "--delete-branch"], returncode=0)
    assert run(["pr", "ship", "--no-wait"]) == 0


def test_merge_force_skips_preflight(fake_cli):
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json(isDraft=True))
    fake_cli.set(["gh", "pr", "merge", "--squash"], returncode=0)
    assert run(["pr", "merge", "--force"]) == 0


# ---------------------------------------------------------------------------
# check — target branch + ticket reference

def test_check_shows_target_and_found_ticket(fake_cli, capsys):
    fake_cli.fail(["git", "remote", "get-url", "origin"], stderr="not a git repository")
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(title="Fix widget rendering", body="Fixes #42"),
    )
    rc = run(["pr", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "target main  <- feature" in out
    assert "ticket" in out and "#42" in out
    assert "no known checkout" in out


def test_check_fails_when_no_ticket_reference(fake_cli, capsys):
    fake_cli.fail(["git", "remote", "get-url", "origin"], stderr="not a git repository")
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(title="Fix widget rendering", body="no ticket mentioned here"),
    )
    rc = run(["pr", "check"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "no reference found" in out


def test_check_notes_unlinked_jira_key(fake_cli, capsys):
    fake_cli.fail(["git", "remote", "get-url", "origin"], stderr="not a git repository")
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(title="Fix widget rendering", body="Implements ABC-123"),
    )
    rc = run(["pr", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ABC-123" in out
    assert "consider linking" in out


def test_check_shows_local_checkout_when_registered(fake_cli, capsys, tmp_path):
    from zithub import registry

    checkout = tmp_path / "widgets-checkout"
    checkout.mkdir()
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:acme/widgets.git")
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(title="Fix widget rendering", body="Fixes #42", headRefName="feature"),
    )
    registry.record_seen(str(checkout), "acme/widgets", "feature")
    fake_cli.set(["git", "-C", str(checkout), "status", "--short"], stdout="")

    rc = run(["pr", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"local  {checkout}" in out
    assert "dirty" not in out
    assert "no known checkout" not in out


def test_check_warns_when_local_checkout_dirty(fake_cli, capsys, tmp_path):
    from zithub import registry

    checkout = tmp_path / "widgets-checkout"
    checkout.mkdir()
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:acme/widgets.git")
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(title="Fix widget rendering", body="Fixes #42", headRefName="feature"),
    )
    registry.record_seen(str(checkout), "acme/widgets", "feature")
    fake_cli.set(["git", "-C", str(checkout), "status", "--short"], stdout=" M some_file.py\n")

    rc = run(["pr", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dirty" in out
    assert "1 uncommitted change" in out


def test_check_parses_repo_from_pr_url(fake_cli, capsys, tmp_path):
    """A full PR URL's owner/repo is parsed directly, without needing the
    current directory to be a checkout of that repo at all."""
    from zithub import registry

    checkout = tmp_path / "widgets-checkout"
    checkout.mkdir()
    registry.record_seen(str(checkout), "acme/widgets", "feature")
    fake_cli.set(
        ["gh", "pr", "view", "https://github.com/acme/widgets/pull/7", "--json", _PR_FIELDS],
        stdout=pr_json(title="Fix widget rendering", body="Fixes #42", headRefName="feature"),
    )
    fake_cli.set(["git", "-C", str(checkout), "status", "--short"], stdout="")

    rc = run(["pr", "check", "https://github.com/acme/widgets/pull/7"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"local  {checkout}" in out
    assert not any(c == ["git", "remote", "get-url", "origin"] for c in fake_cli.calls)


def test_check_multiple_prs_reports_each_and_aggregates_failure(fake_cli, capsys):
    fake_cli.fail(["git", "remote", "get-url", "origin"], stderr="not a git repository")
    fake_cli.set(
        ["gh", "pr", "view", "1", "--json", _PR_FIELDS],
        stdout=pr_json(number=1, title="Fix A", body="Fixes #1"),
    )
    fake_cli.set(
        ["gh", "pr", "view", "2", "--json", _PR_FIELDS],
        stdout=pr_json(number=2, title="Fix B", body="no ticket here"),
    )
    rc = run(["pr", "check", "1", "2"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "#1 Fix A" in out
    assert "#2 Fix B" in out
    assert "no reference found" in out


# ---------------------------------------------------------------------------
# board — local sqlite cache of open PRs

def _board_list_args(host):
    from zithub import gh as gh_mod

    return ["gh", "api", "--hostname", host, "graphql", "-f", f"query={gh_mod._BOARD_PR_LIST_QUERY}", "-F", "q=is:pr is:open author:@me"]


def _board_details_args(host, ids):
    from zithub import gh as gh_mod

    args = ["gh", "api", "--hostname", host, "graphql", "-f", f"query={gh_mod._BOARD_PR_DETAILS_QUERY}"]
    for i in ids:
        args += ["-f", f"ids[]={i}"]
    return args


def _board_list_json(*refs):
    return json.dumps(
        {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {"id": pr_id, "number": n, "updatedAt": updated, "repository": {"nameWithOwner": "acme/widgets"}}
                        for pr_id, n, updated in refs
                    ],
                }
            }
        }
    )


def _board_detail_node(pr_id, number, updated="2026-09-01T00:00:00Z", rollup="SUCCESS"):
    return {
        "id": pr_id,
        "number": number,
        "title": f"Fix widget {number}",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": updated,
        "repository": {"nameWithOwner": "acme/widgets"},
        "comments": {
            "totalCount": 1,
            "nodes": [{"createdAt": "2026-09-10T00:00:00Z", "author": {"login": "reviewer1"}}],
        },
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup}}}]},
    }


def _set_auth_status(fake_cli):
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps(
            {
                "hosts": {
                    "github.com": [{"login": "vivainio", "active": True}],
                    "ghe.example.com": [{"login": "vvainio", "active": True}],
                }
            }
        ),
    )


def test_board_sync_then_list_and_query(fake_cli, capsys, monkeypatch):
    from zithub import board

    monkeypatch.delenv("GH_HOST", raising=False)
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@ghe.example.com:acme/widgets.git")
    fake_cli.set(_board_list_args("ghe.example.com"), stdout=_board_list_json(("PR_1", 1, "2026-09-01T00:00:00Z")))
    fake_cli.set(
        _board_details_args("ghe.example.com", ["PR_1"]),
        stdout=json.dumps({"data": {"nodes": [_board_detail_node("PR_1", 1)]}}),
    )
    _set_auth_status(fake_cli)

    rc = run(["board", "sync"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "synced 1 open PR(s) from ghe.example.com (1 refreshed, 0 unchanged)" in out
    assert board.list_board("ghe.example.com")[0].last_comment_author == "reviewer1"
    assert board.get_login("ghe.example.com") == "vvainio"
    assert board.list_board("github.com") == []

    rc = run(["board"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "acme/widgets#1" in out
    assert "review: approved" in out
    assert "last comment: reviewer1" in out

    rc = run(["board", "query", "select repo, number, ci_state from prs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "acme/widgets" in out
    assert "success" in out


def test_board_sync_skips_unchanged_and_resumes_after_failure(fake_cli, capsys, monkeypatch):
    from zithub import board

    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setattr("zithub.gh.BOARD_DETAILS_BATCH_SIZE", 1)
    _set_auth_status(fake_cli)
    refs = [("PR_1", 1, "t1"), ("PR_2", 2, "t1"), ("PR_3", 3, "t1")]
    fake_cli.set(_board_list_args("github.com"), stdout=_board_list_json(*refs))
    fake_cli.set(
        _board_details_args("github.com", ["PR_1"]),
        stdout=json.dumps({"data": {"nodes": [_board_detail_node("PR_1", 1, updated="t1")]}}),
    )
    fake_cli.set(
        _board_details_args("github.com", ["PR_2"]),
        stdout=json.dumps({"data": {"nodes": [_board_detail_node("PR_2", 2, updated="t1", rollup="PENDING")]}}),
    )
    fake_cli.fail(_board_details_args("github.com", ["PR_3"]), stderr="gh: Not Found (HTTP 404)")

    rc = run(["board", "sync"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "saved 2/3 PR(s) needing refresh" in err
    assert sorted(r.number for r in board.list_board("github.com")) == [1, 2]

    # Re-run: #1 is unchanged and skipped; #2's CI was pending so it's
    # refetched; #3 was never saved so it's fetched now.
    fake_cli.calls.clear()
    fake_cli._responses.pop(tuple(_board_details_args("github.com", ["PR_3"])))
    fake_cli.set(
        _board_details_args("github.com", ["PR_3"]),
        stdout=json.dumps({"data": {"nodes": [_board_detail_node("PR_3", 3, updated="t1")]}}),
    )
    rc = run(["board", "sync"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "synced 3 open PR(s) from github.com (2 refreshed, 1 unchanged)" in out
    assert _board_details_args("github.com", ["PR_1"]) not in fake_cli.calls
    assert sorted(r.number for r in board.list_board("github.com")) == [1, 2, 3]


def test_board_query_rejects_write_statements(fake_cli, capsys, monkeypatch):
    monkeypatch.setenv("GH_HOST", "github.com")
    rc = run(["board", "query", "delete from prs"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "only SELECT/WITH queries are allowed" in err


def test_board_focus_groups_by_what_needs_action(capsys, monkeypatch):
    from zithub import board, gh as gh_mod

    monkeypatch.setenv("GH_HOST", "github.com")

    def pr(number, **overrides):
        base = gh_mod.BoardPr(
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

    board.sync(
        "github.com",
        [
            pr(1, ci_state="failed"),  # fix CI
            pr(2, last_comment_author="someone_else", last_comment_at="2026-09-05T00:00:00Z"),  # needs your reply
            pr(3, review_decision="APPROVED", ci_state="success"),  # ready to merge
            pr(4, review_decision="REVIEW_REQUIRED"),  # waiting on others
        ],
        synced_at="t",
        login="vivainio",
    )

    rc = run(["board", "focus"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "fix CI (1)" in out
    assert "#1" in out
    assert "needs your reply (1)" in out
    assert "#2" in out
    assert "ready to merge (1)" in out
    assert "#3" in out
    assert "waiting on others: 1 PR(s)" in out


def test_board_bare_with_nothing_synced(capsys, monkeypatch):
    monkeypatch.setenv("GH_HOST", "github.com")
    rc = run(["board"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run `zh board sync` first" in out


# ---------------------------------------------------------------------------
# release preflight / create

_RUN_LIST_FIELDS = "databaseId,name,status,conclusion,url,headSha,createdAt"


def set_preflight_up_to_ci(fake_cli, branch="main", sha="abc123", dirty=""):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout=branch)
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:acme/widgets.git")
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps({"hosts": {"github.com": [{"login": "vivainio", "active": True}]}}),
    )
    fake_cli.set(
        ["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"],
        stdout=repo_json(),
    )
    fake_cli.set(
        ["gh", "release", "list", "--limit", "5", "--json", "tagName,name,publishedAt,isDraft,isPrerelease"],
        stdout="[]",
    )
    fake_cli.set(["git", "status", "--short"], stdout=dirty)
    fake_cli.set(["git", "fetch", "origin", "--prune", "--tags"], returncode=0)
    fake_cli.set(["git", "rev-parse", "HEAD"], stdout=sha)
    fake_cli.set(["git", "rev-parse", f"origin/{branch}"], stdout=sha)


def set_ci_success(fake_cli, sha="abc123"):
    fake_cli.set(
        ["gh", "run", "list", "--commit", sha, "--limit", "100", "--json", _RUN_LIST_FIELDS],
        stdout=json.dumps(
            [{"databaseId": 1, "name": "build", "status": "completed", "conclusion": "success", "url": "u1", "headSha": sha}]
        ),
    )


def test_release_bare_pass_no_previous_release(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    set_ci_success(fake_cli)
    fake_cli.set(["git", "log", "-50", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PREFLIGHT PASS" in out
    assert "no previous release" in out
    assert "zh release create <version>" in out


def test_release_bare_suggests_bumps(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    fake_cli._responses[
        ("gh", "release", "list", "--limit", "5", "--json", "tagName,name,publishedAt,isDraft,isPrerelease")
    ] = [(json.dumps([{"tagName": "v1.2.3", "name": "v1.2.3", "publishedAt": "t", "isDraft": False, "isPrerelease": False}]), 0, "")]
    set_ci_success(fake_cli)
    fake_cli.set(["git", "log", "v1.2.3..HEAD", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "zh release create v1.2.4 --notes" in out and "(patch)" in out
    assert "zh release create v1.3.0 --notes" in out and "(minor)" in out
    assert "zh release create v2.0.0 --notes" in out and "(major)" in out


def test_release_bare_branch_mismatch(fake_cli, capsys):
    set_preflight_up_to_ci(fake_cli, branch="feature")
    rc = run(["release"])
    assert rc == 1
    assert "release target is 'main'" in capsys.readouterr().err


def test_release_bare_dirty_warns_but_passes(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli, dirty="?? scratch.txt\n")
    set_ci_success(fake_cli)
    fake_cli.set(["git", "log", "-50", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "do not release until everything is committed" in out
    assert "PREFLIGHT PASS" in out


def test_release_bare_sync_mismatch_ahead_suggests_push(fake_cli, capsys):
    set_preflight_up_to_ci(fake_cli)
    fake_cli._responses[("git", "rev-parse", "origin/main")] = [("def456", 0, "")]
    fake_cli.set(["git", "rev-list", "--left-right", "--count", "HEAD...def456"], stdout="1\t0")
    rc = run(["release"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "!= origin/main" in err
    assert "push with `git push origin main`" in err


def test_release_bare_sync_mismatch_behind_suggests_pull(fake_cli, capsys):
    set_preflight_up_to_ci(fake_cli)
    fake_cli._responses[("git", "rev-parse", "origin/main")] = [("def456", 0, "")]
    fake_cli.set(["git", "rev-list", "--left-right", "--count", "HEAD...def456"], stdout="0\t1")
    rc = run(["release"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "pull with `git pull origin main`" in err


def test_release_bare_sync_mismatch_diverged_suggests_reconcile(fake_cli, capsys):
    set_preflight_up_to_ci(fake_cli)
    fake_cli._responses[("git", "rev-parse", "origin/main")] = [("def456", 0, "")]
    fake_cli.set(["git", "rev-list", "--left-right", "--count", "HEAD...def456"], stdout="2\t3")
    rc = run(["release"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "reconcile the diverged history" in err


def test_release_bare_no_ci_found_proceeds(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    fake_cli.set(
        ["gh", "run", "list", "--commit", "abc123", "--limit", "100", "--json", _RUN_LIST_FIELDS],
        stdout="[]",
    )
    fake_cli.set(
        ["gh", "run", "list", "--branch", "main", "--limit", "5", "--json", _RUN_LIST_FIELDS],
        stdout="[]",
    )
    fake_cli.set(["git", "log", "-50", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "proceed with judgement" in out
    assert "PREFLIGHT PASS" in out


def test_release_bare_ci_failed(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    fake_cli.set(
        ["gh", "run", "list", "--commit", "abc123", "--limit", "100", "--json", _RUN_LIST_FIELDS],
        stdout=json.dumps(
            [{"databaseId": 1, "name": "build", "status": "completed", "conclusion": "failure", "url": "u1", "headSha": "abc123"}]
        ),
    )
    fake_cli.set(["gh", "run", "view", "1", "--log-failed"], stdout="boom at line 42")
    rc = run(["release"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "failed" in err
    assert "boom at line 42" in out


def _set_no_publish_workflow(fake_cli):
    fake_cli.set(
        ["gh", "run", "list", "--event", "release", "--limit", "10", "--json", _RUN_LIST_FIELDS],
        stdout="[]",
    )


def test_release_create_preflight_flag_runs_preflight_then_creates(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    set_ci_success(fake_cli)
    fake_cli.set(
        ["gh", "release", "create", "v1.0.0", "--notes", "first release", "--title", "v1.0.0", "--target", "main"],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    _set_no_publish_workflow(fake_cli)
    rc = run(["release", "create", "v1.0.0", "-n", "first release", "-t", "v1.0.0", "--preflight"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "released:" in out


def test_release_create_skips_preflight_by_default(fake_cli, monkeypatch):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    fake_cli.set(
        ["gh", "release", "create", "v1.0.0", "--notes", "notes", "--title", "v1.0.0"],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    _set_no_publish_workflow(fake_cli)
    rc = run(["release", "create", "v1.0.0", "-n", "notes"])
    assert rc == 0


def test_release_create_waits_for_publish_workflow_success(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod
    from datetime import datetime, timezone

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_cli.set(
        ["gh", "release", "create", "v1.0.0", "--notes", "notes", "--title", "v1.0.0"],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    fake_cli.set(
        ["gh", "run", "list", "--event", "release", "--limit", "10", "--json", _RUN_LIST_FIELDS],
        stdout=json.dumps(
            [
                {
                    "databaseId": 1,
                    "name": "Publish to PyPI",
                    "status": "completed",
                    "conclusion": "success",
                    "url": "u1",
                    "headSha": "abc",
                    "createdAt": created_at,
                }
            ]
        ),
    )
    rc = run(["release", "create", "v1.0.0", "-n", "notes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "publish success (Publish to PyPI)" in out


def test_release_create_reports_publish_workflow_failure(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod
    from datetime import datetime, timezone

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_cli.set(
        ["gh", "release", "create", "v1.0.0", "--notes", "notes", "--title", "v1.0.0"],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    fake_cli.set(
        ["gh", "run", "list", "--event", "release", "--limit", "10", "--json", _RUN_LIST_FIELDS],
        stdout=json.dumps(
            [
                {
                    "databaseId": 1,
                    "name": "Publish to PyPI",
                    "status": "completed",
                    "conclusion": "failure",
                    "url": "u1",
                    "headSha": "abc",
                    "createdAt": created_at,
                }
            ]
        ),
    )
    fake_cli.set(["gh", "run", "view", "1", "--log-failed"], stdout="pypi rejected upload")
    rc = run(["release", "create", "v1.0.0", "-n", "notes"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "released:" in out
    assert "release workflow(s) failed" in err


def test_release_create_requires_notes(fake_cli):
    with pytest.raises(SystemExit) as exc:
        run(["release", "create", "v1.0.0"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# release patch/minor/major

def _set_previous_release(fake_cli, tag="v1.2.3"):
    fake_cli._responses[
        ("gh", "release", "list", "--limit", "5", "--json", "tagName,name,publishedAt,isDraft,isPrerelease")
    ] = [(json.dumps([{"tagName": tag, "name": tag, "publishedAt": "t", "isDraft": False, "isPrerelease": False}]), 0, "")]


def test_release_patch_shows_computed_version_and_gh_hint(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    _set_previous_release(fake_cli)
    set_ci_success(fake_cli)
    fake_cli.set(["git", "log", "v1.2.3..HEAD", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release", "patch"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v1.2.3 -> v1.2.4" in out
    assert 'zh release create v1.2.4 --notes "..." --title v1.2.4' in out


def test_release_minor_never_creates(fake_cli, monkeypatch, capsys):
    """`zh release minor` only ever previews — it must never call `gh
    release create` itself, even implicitly."""
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    _set_previous_release(fake_cli)
    set_ci_success(fake_cli)
    fake_cli.set(["git", "log", "v1.2.3..HEAD", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release", "minor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v1.2.3 -> v1.3.0" in out
    assert 'zh release create v1.3.0 --notes "..." --title v1.3.0' in out
    assert not any(c[:3] == ["gh", "release", "create"] for c in fake_cli.calls)


def test_release_major_force_uses_recent_releases_without_preflight(fake_cli):
    fake_cli.set(
        ["gh", "release", "list", "--limit", "1", "--json", "tagName,name,publishedAt,isDraft,isPrerelease"],
        stdout=json.dumps([{"tagName": "v1.2.3", "name": "v1.2.3", "publishedAt": "t", "isDraft": False, "isPrerelease": False}]),
    )
    fake_cli.set(["git", "log", "v1.2.3..HEAD", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release", "major", "--force"])
    assert rc == 0
    assert not any(c[:3] == ["gh", "release", "create"] for c in fake_cli.calls)


def test_release_bump_target_flag_only_when_explicit(fake_cli, capsys):
    fake_cli.set(
        ["gh", "release", "list", "--limit", "1", "--json", "tagName,name,publishedAt,isDraft,isPrerelease"],
        stdout=json.dumps([{"tagName": "v1.2.3", "name": "v1.2.3", "publishedAt": "t", "isDraft": False, "isPrerelease": False}]),
    )
    fake_cli.set(["git", "log", "v1.2.3..HEAD", "--oneline", "--no-decorate"], stdout="")
    rc = run(["release", "patch", "--target", "release-2.0", "--force"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--target release-2.0" in out


def test_release_bump_no_previous_release_errors(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    set_ci_success(fake_cli)
    rc = run(["release", "patch"])
    assert rc == 1
    assert "no previous release to bump from" in capsys.readouterr().err


def test_release_bump_non_semver_tag_errors(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    _set_previous_release(fake_cli, tag="release-2026-01")
    set_ci_success(fake_cli)
    rc = run(["release", "patch"])
    assert rc == 1
    assert "doesn't look like a plain semver tag" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# repos — local-checkout registry

def test_repos_lists_known_checkouts(monkeypatch, capsys):
    from zithub import registry

    entries = [registry.RepoEntry(path="/home/v/r/zithub", repo="vivainio/zithub", branch="main", last_seen=0.0)]
    monkeypatch.setattr(registry, "list_seen", lambda: entries)
    rc = run(["repos"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "vivainio/zithub" in out
    assert "main" in out


def test_repos_query_filters(monkeypatch):
    from zithub import registry

    calls = []
    monkeypatch.setattr(registry, "find_seen", lambda q: calls.append(q) or [])
    run(["repos", "zithub"])
    assert calls == ["zithub"]


def test_repos_empty_is_an_error(fake_cli, monkeypatch, capsys):
    from zithub import registry

    monkeypatch.setattr(registry, "list_seen", lambda: [])
    rc = run(["repos"])
    assert rc == 1
    assert "no known checkouts" in capsys.readouterr().err


def test_main_records_repo_seen_for_gh_commands(monkeypatch, fake_cli):
    import zithub.cli as cli_mod

    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:acme/widgets.git")
    fake_cli.set(["git", "rev-parse", "--show-toplevel"], stdout="/home/v/r/widgets")
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="main")

    recorded = []
    monkeypatch.setattr(
        cli_mod.registry, "record_seen", lambda path, repo, branch: recorded.append((path, repo, branch))
    )
    monkeypatch.setattr(cli_mod.gh, "ensure_gh_account_for_repo", lambda: None)
    monkeypatch.setattr("sys.argv", ["zh", "pr", "check"])
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())

    with pytest.raises(SystemExit):
        cli_mod.main()

    assert recorded == [("/home/v/r/widgets", "acme/widgets", "main")]


def test_main_does_not_record_for_repos_command(monkeypatch, fake_cli):
    import zithub.cli as cli_mod

    recorded = []
    monkeypatch.setattr(
        cli_mod.registry, "record_seen", lambda path, repo, branch: recorded.append((path, repo, branch))
    )
    monkeypatch.setattr(cli_mod.registry, "list_seen", lambda: [])
    monkeypatch.setattr("sys.argv", ["zh", "repos"])

    with pytest.raises(SystemExit):
        cli_mod.main()

    assert recorded == []


def test_merge_pending_without_wait_blocks(fake_cli, capsys):
    fake_cli.set(
        ["gh", "pr", "view", "--json", _PR_FIELDS],
        stdout=pr_json(
            statusCheckRollup=[{"name": "build", "status": "IN_PROGRESS", "conclusion": None}]
        ),
    )
    fake_cli.set(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"], stdout=repo_json())
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._REVIEW_THREADS_QUERY}",
            "-F",
            "owner=acme",
            "-F",
            "repo=widgets",
            "-F",
            "pr=7",
        ],
        stdout=empty_threads_json(),
    )
    rc = run(["pr", "merge", "--no-wait"])
    assert rc == 1
    assert "still pending" in capsys.readouterr().err


def test_bare_zh_prints_help_and_does_nothing(monkeypatch, fake_cli, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr("sys.argv", ["zh"])
    with pytest.raises(SystemExit) as exc:
        cli_mod.main()

    assert exc.value.code == 0
    assert "usage: zh" in capsys.readouterr().out
    assert fake_cli.calls == []


# ---------------------------------------------------------------------------
# gh / git — passthrough with the repo owner's GH_TOKEN


def _set_two_accounts(fake_cli, origin):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout=origin)
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps(
            {
                "hosts": {
                    "github.com": [
                        {"login": "villevai_Basware", "active": True},
                        {"login": "vivainio", "active": False},
                    ]
                }
            }
        ),
    )
    fake_cli.set(["gh", "auth", "token", "--hostname", "github.com", "--user", "vivainio"], stdout="tok-vivainio")


@pytest.fixture
def passthrough_calls(monkeypatch):
    from zithub import gh as gh_mod

    calls = []

    def fake(args, env=None):
        calls.append((args, env))
        return 0

    monkeypatch.setattr(gh_mod, "run_passthrough", fake)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return calls


def test_gh_passthrough_uses_repo_owner_token(fake_cli, passthrough_calls, monkeypatch, capsys):
    from zithub import cli

    _set_two_accounts(fake_cli, "https://github.com/vivainio/zithub.git")
    monkeypatch.setattr("sys.argv", ["zh", "gh", "-R", "vivainio/zithub", "pr", "list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    [(args, env)] = passthrough_calls
    assert args == ["gh", "-R", "vivainio/zithub", "pr", "list"]
    assert env["GH_TOKEN"] == "tok-vivainio"
    assert "using gh account vivainio" in capsys.readouterr().err


def test_gh_passthrough_respects_explicit_gh_token(fake_cli, passthrough_calls, monkeypatch):
    _set_two_accounts(fake_cli, "https://github.com/vivainio/zithub.git")
    monkeypatch.setenv("GH_TOKEN", "mine")
    assert run(["gh", "pr", "list"]) == 0
    assert passthrough_calls == [(["gh", "pr", "list"], None)]


def test_git_over_https_uses_gh_helper_and_owner_token(fake_cli, passthrough_calls):
    _set_two_accounts(fake_cli, "https://github.com/vivainio/zithub.git")
    assert run(["git", "push", "origin", "main"]) == 0
    [(args, env)] = passthrough_calls
    assert args == [
        "git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push", "origin", "main",
    ]
    assert env["GH_TOKEN"] == "tok-vivainio"


def test_git_over_ssh_is_plain_git(fake_cli, passthrough_calls):
    _set_two_accounts(fake_cli, "git@github.com:vivainio/zithub.git")
    assert run(["git", "push"]) == 0
    assert passthrough_calls == [(["git", "push"], None)]


def test_gh_passthrough_looks_up_accounts_on_origins_host(fake_cli, passthrough_calls):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="https://ghe.example.com/acme/widgets.git")
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps({"hosts": {"ghe.example.com": [{"login": "acme", "active": False}]}}),
    )
    fake_cli.set(["gh", "auth", "token", "--hostname", "ghe.example.com", "--user", "acme"], stdout="tok-ghe")
    assert run(["gh", "pr", "list"]) == 0
    [(_args, env)] = passthrough_calls
    assert env["GH_TOKEN"] == "tok-ghe"
