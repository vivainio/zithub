"""End-to-end: argparse -> cmd_* -> gh.* -> subprocess, via fake_cli."""

from __future__ import annotations

import json

import pytest

from zithub import gh
from zithub.cli import build_parser

_PR_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName"
)


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
# passthroughs build the right gh argv

def test_create_builds_gh_command(fake_cli):
    fake_cli.set(
        [
            "gh",
            "pr",
            "create",
            "--title",
            "t",
            "--body",
            "b",
            "--draft",
            "--label",
            "bug",
            "--reviewer",
            "alice",
        ],
        returncode=0,
    )
    rc = run(
        [
            "pr",
            "create",
            "-t",
            "t",
            "-b",
            "b",
            "-d",
            "-l",
            "bug",
            "-r",
            "alice",
        ]
    )
    assert rc == 0


def test_close_builds_gh_command(fake_cli):
    fake_cli.set(["gh", "pr", "close", "42", "--comment", "nvm", "--delete-branch"], returncode=0)
    assert run(["pr", "close", "42", "-c", "nvm", "-d"]) == 0


def test_comment_builds_gh_command(fake_cli):
    fake_cli.set(["gh", "pr", "comment", "--body", "hi"], returncode=0)
    assert run(["pr", "comment", "-b", "hi"]) == 0


def test_review_defaults_to_comment(fake_cli):
    fake_cli.set(["gh", "pr", "review", "--comment", "--body", "lgtm-ish"], returncode=0)
    assert run(["pr", "review", "-b", "lgtm-ish"]) == 0


def test_review_approve(fake_cli):
    fake_cli.set(["gh", "pr", "review", "--approve"], returncode=0)
    assert run(["pr", "review", "--approve"]) == 0


def test_label_requires_add_or_remove(fake_cli, capsys):
    rc = run(["pr", "label"])
    assert rc == 1
    assert "pass --add and/or --remove" in capsys.readouterr().err


def test_label_add_and_remove(fake_cli):
    fake_cli.set(
        ["gh", "pr", "edit", "9", "--add-label", "bug", "--remove-label", "wontfix"],
        returncode=0,
    )
    assert run(["pr", "label", "9", "--add", "bug", "--remove", "wontfix"]) == 0


def test_reviewer_add(fake_cli):
    fake_cli.set(["gh", "pr", "edit", "--add-reviewer", "bob"], returncode=0)
    assert run(["pr", "reviewer", "--add", "bob"]) == 0


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
