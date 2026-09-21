"""End-to-end tests for the read-side status/ci/pr/my/review/issues commands
ported from wazup, via fake_cli -- mirrors test_cli.py's pattern."""

from __future__ import annotations

import json
from datetime import date, timedelta

from zithub import gh
from zithub.cli import build_parser

_PR_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName,body"
)
_REPO_FIELDS = "owner,name,nameWithOwner,url,defaultBranchRef"


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


def _since(days: int = 7) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# status

def test_status_clean_pr_all_checks_pass(fake_cli, capsys):
    fake_cli.set(["git", "fetch"], stdout="")
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=repo_json())
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="feature")
    fake_cli.set(["git", "status", "--porcelain=v2", "--branch"], stdout="# branch.ab +0 -0")
    fake_cli.set(["git", "rev-parse", "--git-dir", "--git-common-dir"], stdout="x\nx")
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(["gh", "run", "list", "--limit", "20", "--json", gh._RUN_LIST_FIELDS], stdout="[]")

    rc = run([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "repo   acme/widgets" in out
    assert "branch feature" in out
    assert "local  pushed, clean" in out
    assert "pr     #7 Add widget" in out
    assert "Everything is clean" in out


def test_status_no_pr_falls_back_to_branch_ci(fake_cli, capsys):
    fake_cli.set(["git", "fetch"], stdout="")
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=repo_json())
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="feature")
    fake_cli.set(["git", "status", "--porcelain=v2", "--branch"], stdout="# branch.ab +0 -0")
    fake_cli.set(["git", "rev-parse", "--git-dir", "--git-common-dir"], stdout="x\nx")
    fake_cli.fail(["gh", "pr", "view", "--json", _PR_FIELDS])
    fake_cli.set(
        ["gh", "run", "list", "--branch", "feature", "--limit", "10", "--json", gh._RUN_LIST_FIELDS],
        stdout="[]",
    )

    rc = run([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pr     none" in out
    assert "ci     none" in out


# ---------------------------------------------------------------------------
# ci

def test_ci_with_pr_shows_checks(fake_cli, capsys):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="feature")
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(["gh", "run", "list", "--limit", "20", "--json", gh._RUN_LIST_FIELDS], stdout="[]")

    rc = run(["ci"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pr #7 Add widget" in out
    assert "build" in out


def test_ci_no_pr_falls_back_to_branch_runs(fake_cli, capsys):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="feature")
    fake_cli.fail(["gh", "pr", "view", "--json", _PR_FIELDS])
    fake_cli.set(
        ["gh", "run", "list", "--branch", "feature", "--limit", "10", "--json", gh._RUN_LIST_FIELDS],
        stdout=json.dumps(
            [
                {
                    "databaseId": 1,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://x/1",
                    "headSha": "abc123",
                }
            ]
        ),
    )
    fake_cli.set(["gh", "run", "list", "--limit", "20", "--json", gh._RUN_LIST_FIELDS], stdout="[]")

    rc = run(["ci"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "branch feature  (no open PR)" in out
    assert "build" in out


# ---------------------------------------------------------------------------
# pr (bare status dashboard)

def test_pr_status_shows_checks_and_threads(fake_cli, capsys):
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=repo_json())
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="feature")
    fake_cli.set(["gh", "pr", "view", "--json", _PR_FIELDS], stdout=pr_json())
    fake_cli.set(["gh", "run", "list", "--limit", "20", "--json", gh._RUN_LIST_FIELDS], stdout="[]")
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
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                                            "nodes": [
                                                {
                                                    "id": "C1",
                                                    "body": "please fix",
                                                    "createdAt": "2026-01-01T00:00:00Z",
                                                    "path": "a.py",
                                                    "line": 3,
                                                    "author": {"login": "bob"},
                                                }
                                            ],
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            }
        ),
    )

    rc = run(["pr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pr     #7 Add widget" in out
    assert "comments  1 unresolved" in out
    assert "please fix" in out


def test_pr_status_no_pr_errors(fake_cli, capsys):
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=repo_json())
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="feature")
    fake_cli.fail(["gh", "pr", "view", "--json", _PR_FIELDS])

    fake_cli.set(
        ["gh", "pr", "list", "--author", "@me", "--json", "number,title,url,state,isDraft,updatedAt"],
        stdout=json.dumps(
            [
                {
                    "number": 3,
                    "title": "Fix bug",
                    "url": "https://x/3",
                    "state": "OPEN",
                    "isDraft": False,
                    "updatedAt": "2026-01-01T00:00:00Z",
                }
            ]
        ),
    )

    rc = run(["pr"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "branch feature  (no open PR)" in out
    assert "your open PRs in acme/widgets:" in out
    assert "#3 Fix bug" in out


# ---------------------------------------------------------------------------
# my / review / issues

def test_my_this_lists_open_prs_in_current_repo(fake_cli, capsys):
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=repo_json())
    fake_cli.set(
        ["gh", "pr", "list", "--author", "@me", "--json", "number,title,url,state,isDraft,updatedAt"],
        stdout=json.dumps(
            [
                {
                    "number": 3,
                    "title": "Fix bug",
                    "url": "https://x/3",
                    "state": "OPEN",
                    "isDraft": False,
                    "updatedAt": "2026-01-01T00:00:00Z",
                }
            ]
        ),
    )

    rc = run(["my", "--this"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "your open PRs in acme/widgets:" in out
    assert "#3 Fix bug" in out


def test_my_shows_recent_activity_across_repos(fake_cli, capsys):
    since = _since(30)
    fake_cli.set(
        [
            "gh",
            "search",
            "prs",
            "--author",
            "@me",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title,url,state,isDraft,updatedAt,repository",
        ],
        stdout=json.dumps(
            [
                {
                    "number": 11,
                    "title": "Cross-repo fix",
                    "url": "https://x/11",
                    "state": "OPEN",
                    "isDraft": False,
                    "updatedAt": "2026-01-01T00:00:00Z",
                    "repository": {"nameWithOwner": "other/repo"},
                }
            ]
        ),
    )
    fake_cli.set(
        [
            "gh",
            "search",
            "prs",
            "--author",
            "@me",
            "--state",
            "closed",
            "--updated",
            f">={since}",
            "--sort",
            "updated",
            "--limit",
            "200",
            "--json",
            "number,title,url,state,isDraft,updatedAt,repository",
        ],
        stdout=json.dumps([]),
    )

    rc = run(["my"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"your open PRs across all repos, plus closed/merged since {since}:" in out
    assert "other/repo" in out
    assert "#11 Cross-repo fix" in out


def test_review_lists_prs_awaiting_review(fake_cli, capsys):
    since = _since()
    fake_cli.set(
        [
            "gh",
            "search",
            "prs",
            "--review-requested",
            "@me",
            "--state",
            "open",
            "--updated",
            f">={since}",
            "--sort",
            "updated",
            "--json",
            "number,title,url,state,isDraft,updatedAt,repository",
        ],
        stdout=json.dumps(
            [
                {
                    "number": 5,
                    "title": "Needs review",
                    "url": "https://x/5",
                    "state": "OPEN",
                    "isDraft": False,
                    "updatedAt": "2026-01-01T00:00:00Z",
                    "repository": {"nameWithOwner": "acme/widgets"},
                }
            ]
        ),
    )

    rc = run(["review"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"updated since {since}" in out
    assert "acme/widgets" in out
    assert "#5 Needs review" in out


def test_issues_lists_open_issues_in_repo(fake_cli, capsys):
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=repo_json())
    fake_cli.set(
        ["gh", "issue", "list", "--assignee", "@me", "--json", "number,title,url,state,updatedAt"],
        stdout=json.dumps(
            [
                {
                    "number": 9,
                    "title": "Broken thing",
                    "url": "https://x/9",
                    "state": "OPEN",
                    "updatedAt": "2026-01-01T00:00:00Z",
                }
            ]
        ),
    )

    rc = run(["issues"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "your open issues in acme/widgets:" in out
    assert "#9 Broken thing" in out


def test_issues_outside_repo_shows_recent_activity(fake_cli, capsys):
    fake_cli.fail(["gh", "repo", "view", "--json", _REPO_FIELDS])
    since = _since()
    fake_cli.set(
        [
            "gh",
            "search",
            "issues",
            "--assignee",
            "@me",
            "--state",
            "open",
            "--updated",
            f">={since}",
            "--sort",
            "updated",
            "--json",
            "number,title,url,state,updatedAt,repository",
        ],
        stdout=json.dumps(
            [
                {
                    "number": 12,
                    "title": "Cross-repo issue",
                    "url": "https://x/12",
                    "state": "OPEN",
                    "updatedAt": "2026-01-01T00:00:00Z",
                    "repository": {"nameWithOwner": "other/repo"},
                }
            ]
        ),
    )

    rc = run(["issues"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"your open issues with activity since {since}:" in out
    assert "other/repo  #12 Cross-repo issue" in out
