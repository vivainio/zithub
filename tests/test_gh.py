"""gh.py functions that shell out to `gh`, exercised via the fake_cli
subprocess.run stand-in (see conftest.py) -- real command lines in, real
parsing logic tested, no actual gh binary involved."""

from __future__ import annotations

import json
import subprocess

import pytest

from zithub import gh


def test_run_raises_zithub_error_when_binary_missing(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(gh.ZithubError, match="not found on PATH"):
        gh.repo_info()


def test_run_raises_zithub_error_on_nonzero_exit(fake_cli):
    fake_cli.fail(
        ["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"],
        stderr="not a repository",
    )
    with pytest.raises(gh.ZithubError, match="not a repository"):
        gh.repo_info()


def test_repo_info(fake_cli):
    fake_cli.set(
        ["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"],
        stdout=json.dumps(
            {
                "owner": {"login": "acme"},
                "name": "widgets",
                "nameWithOwner": "acme/widgets",
                "url": "https://github.com/acme/widgets",
                "defaultBranchRef": {"name": "main"},
            }
        ),
    )
    repo = gh.repo_info()
    assert repo.owner == "acme"
    assert repo.name == "widgets"
    assert repo.name_with_owner == "acme/widgets"
    assert repo.default_branch == "main"


def test_run_passthrough_returns_exit_code(fake_cli):
    fake_cli.set(["gh", "pr", "close", "42"], returncode=0)
    assert gh.run_passthrough(["gh", "pr", "close", "42"]) == 0


def test_run_passthrough_missing_binary(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(gh.ZithubError, match="not found on PATH"):
        gh.run_passthrough(["gh", "pr", "close"])


_PR_JSON = {
    "number": 7,
    "title": "Add widget",
    "url": "https://github.com/acme/widgets/pull/7",
    "state": "OPEN",
    "isDraft": False,
    "reviewDecision": "APPROVED",
    "headRefName": "feature",
    "baseRefName": "main",
    "statusCheckRollup": [
        {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u1"},
        {"name": "lint", "status": "IN_PROGRESS", "conclusion": None, "detailsUrl": "u2"},
    ],
}


def test_resolve_pr_current_branch(fake_cli):
    fields = (
        "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName"
    )
    fake_cli.set(["gh", "pr", "view", "--json", fields], stdout=json.dumps(_PR_JSON))
    pr = gh.resolve_pr()
    assert pr.number == 7
    assert pr.review_decision == "APPROVED"
    assert len(pr.checks) == 2
    assert pr.checks[0].name == "build"
    assert gh.is_success(pr.checks[0])
    assert gh.is_pending(pr.checks[1])


def test_resolve_pr_with_explicit_ref(fake_cli):
    fields = (
        "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName"
    )
    fake_cli.set(["gh", "pr", "view", "42", "--json", fields], stdout=json.dumps(_PR_JSON))
    pr = gh.resolve_pr("42")
    assert pr.number == 7


def test_is_failed():
    failing = gh.CheckRun(name="build", status="COMPLETED", conclusion="FAILURE")
    passing = gh.CheckRun(name="build", status="COMPLETED", conclusion="SUCCESS")
    pending = gh.CheckRun(name="build", status="IN_PROGRESS", conclusion=None)
    assert gh.is_failed(failing) is True
    assert gh.is_failed(passing) is False
    assert gh.is_pending(pending) is True
    assert gh.is_pending(failing) is False


_THREAD_PAGE_1 = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
                    "nodes": [
                        {
                            "id": "T_1",
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "C_1",
                                        "body": "please fix",
                                        "createdAt": "2026-01-01T00:00:00Z",
                                        "path": "a.py",
                                        "line": 10,
                                        "author": {"login": "alice"},
                                    }
                                ]
                            },
                        }
                    ],
                }
            }
        }
    }
}

_THREAD_PAGE_2 = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "T_2",
                            "isResolved": True,
                            "comments": {"nodes": []},
                        }
                    ],
                }
            }
        }
    }
}


def test_review_threads_paginates(fake_cli):
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
        stdout=json.dumps(_THREAD_PAGE_1),
    )
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
            "-F",
            "after=CURSOR1",
        ],
        stdout=json.dumps(_THREAD_PAGE_2),
    )

    threads = gh.review_threads("acme", "widgets", 7)
    assert [t.id for t in threads] == ["T_1", "T_2"]
    assert threads[0].is_resolved is False
    assert threads[0].path == "a.py"
    assert threads[0].line == 10
    assert threads[0].comments[0].author == "alice"
    assert threads[1].is_resolved is True


def test_resolve_thread(fake_cli):
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
    assert gh.resolve_thread("T_1") is True


def test_unresolve_thread(fake_cli):
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
            "-F",
            "id=T_1",
        ],
        stdout=json.dumps({"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}}),
    )
    assert gh.unresolve_thread("T_1") is False


def test_reply_to_thread(fake_cli):
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
            {
                "data": {
                    "addPullRequestReviewThreadReply": {
                        "comment": {"url": "https://github.com/acme/widgets/pull/7#comment"}
                    }
                }
            }
        ),
    )
    url = gh.reply_to_thread("T_1", "done")
    assert url == "https://github.com/acme/widgets/pull/7#comment"
