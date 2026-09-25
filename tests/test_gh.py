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
    fields = gh._PR_VIEW_FIELDS
    fake_cli.set(["gh", "pr", "view", "--json", fields], stdout=json.dumps(_PR_JSON))
    pr = gh.resolve_pr()
    assert pr.number == 7
    assert pr.review_decision == "APPROVED"
    assert len(pr.checks) == 2
    assert pr.checks[0].name == "build"
    assert gh.is_success(pr.checks[0])
    assert gh.is_pending(pr.checks[1])


def test_resolve_pr_with_explicit_ref(fake_cli):
    fields = gh._PR_VIEW_FIELDS
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


def test_review_threads_paginates_comments_within_a_thread(fake_cli):
    """A single thread with more than one page of comments -- the outer
    reviewThreads page has no more pages, but the one thread's own comments
    connection does, requiring a second, thread-scoped query."""
    page = {
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
                                    "pageInfo": {"hasNextPage": True, "endCursor": "CCURSOR1"},
                                    "nodes": [
                                        {
                                            "id": "C_1",
                                            "body": "first",
                                            "createdAt": "2026-01-01T00:00:00Z",
                                            "path": "a.py",
                                            "line": 10,
                                            "author": {"login": "alice"},
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
        stdout=json.dumps(page),
    )
    fake_cli.set(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={gh._THREAD_COMMENTS_QUERY}",
            "-F",
            "id=T_1",
            "-F",
            "after=CCURSOR1",
        ],
        stdout=json.dumps(
            {
                "data": {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "C_2",
                                    "body": "second",
                                    "createdAt": "2026-01-01T01:00:00Z",
                                    "path": "a.py",
                                    "line": 10,
                                    "author": {"login": "bob"},
                                }
                            ],
                        }
                    }
                }
            }
        ),
    )

    threads = gh.review_threads("acme", "widgets", 7)
    [thread] = threads
    assert [c.id for c in thread.comments] == ["C_1", "C_2"]
    assert [c.author for c in thread.comments] == ["alice", "bob"]


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


# ---------------------------------------------------------------------------
# release preflight plumbing

def test_current_branch(fake_cli):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="main\n")
    assert gh.current_branch() == "main"


def test_remote_branch_sha_missing(fake_cli):
    fake_cli.fail(["git", "rev-parse", "origin/main"], stderr="unknown revision")
    assert gh.remote_branch_sha("main") is None


def test_remote_branch_sha_found(fake_cli):
    fake_cli.set(["git", "rev-parse", "origin/main"], stdout="abc123\n")
    assert gh.remote_branch_sha("main") == "abc123"


def test_dirty_files_excludes_uv_lock(fake_cli):
    fake_cli.set(
        ["git", "status", "--short"],
        stdout=" M uv.lock\n M src/zithub/cli.py\n?? scratch.txt\n",
    )
    assert gh.dirty_files() == [" M src/zithub/cli.py", "?? scratch.txt"]


def test_dirty_files_clean(fake_cli):
    fake_cli.set(["git", "status", "--short"], stdout="")
    assert gh.dirty_files() == []


def test_list_gh_accounts(fake_cli):
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps(
            {
                "hosts": {
                    "github.com": [
                        {"login": "vivainio", "active": True},
                        {"login": "villevai_Basware", "active": False},
                    ]
                }
            }
        ),
    )
    accounts = gh.list_gh_accounts()
    assert [(a.login, a.active) for a in accounts] == [
        ("vivainio", True),
        ("villevai_Basware", False),
    ]


def test_ensure_gh_account_for_repo_no_op_when_already_active(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:vivainio/zithub.git")
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps({"hosts": {"github.com": [{"login": "vivainio", "active": True}]}}),
    )
    assert gh.ensure_gh_account_for_repo() is None


def test_ensure_gh_account_for_repo_switches_by_login_match(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:vivainio/zithub.git")
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
    fake_cli.set(
        ["gh", "auth", "switch", "--hostname", "github.com", "--user", "vivainio"], returncode=0
    )
    notice = gh.ensure_gh_account_for_repo()
    assert notice == "switched active gh account to vivainio (owner of vivainio's repos)"


def test_ensure_gh_account_for_repo_switches_by_org_suffix_match(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:Basware/widgets.git")
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps(
            {
                "hosts": {
                    "github.com": [
                        {"login": "vivainio", "active": True},
                        {"login": "villevai_Basware", "active": False},
                    ]
                }
            }
        ),
    )
    fake_cli.set(
        ["gh", "auth", "switch", "--hostname", "github.com", "--user", "villevai_Basware"],
        returncode=0,
    )
    notice = gh.ensure_gh_account_for_repo()
    assert notice == "switched active gh account to villevai_Basware (owner of Basware's repos)"


def test_ensure_gh_account_for_repo_no_matching_account_is_noop(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:someoneelse/widgets.git")
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps({"hosts": {"github.com": [{"login": "vivainio", "active": True}]}}),
    )
    assert gh.ensure_gh_account_for_repo() is None


def test_ensure_gh_account_for_repo_no_remote_is_noop(fake_cli):
    fake_cli.fail(["git", "remote", "get-url", "origin"], stderr="no such remote 'origin'")
    assert gh.ensure_gh_account_for_repo() is None


def test_runs_for_commit_filters_by_sha(fake_cli):
    fake_cli.set(
        ["gh", "run", "list", "--commit", "abc123", "--limit", "100", "--json", gh._RUN_LIST_FIELDS],
        stdout=json.dumps(
            [
                {"databaseId": 1, "name": "build", "status": "completed", "conclusion": "success", "url": "u1", "headSha": "abc123"},
                {"databaseId": 2, "name": "stale", "status": "completed", "conclusion": "success", "url": "u2", "headSha": "def456"},
            ]
        ),
    )
    runs = gh.runs_for_commit("abc123")
    assert [r.name for r in runs] == ["build"]


def test_create_release_builds_command(fake_cli):
    fake_cli.set(
        [
            "gh",
            "release",
            "create",
            "v1.0.0",
            "--notes",
            "notes here",
            "--title",
            "v1.0.0",
            "--target",
            "main",
        ],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    url = gh.create_release(tag="v1.0.0", notes="notes here", title="v1.0.0", target="main")
    assert url == "https://github.com/acme/widgets/releases/tag/v1.0.0"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/acme/widgets.git", "github.com"),
        ("git@ghe.example.com:acme/widgets.git", "ghe.example.com"),
        ("ssh://git@GHE.example.com:2222/acme/widgets", "ghe.example.com"),
        ("git@github-work:acme/widgets.git", "github.com"),  # ssh alias, not a real host
    ],
)
def test_current_host_from_origin(fake_cli, monkeypatch, url, expected):
    monkeypatch.delenv("GH_HOST", raising=False)
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout=url)
    assert gh.current_host() == expected


def test_current_host_prefers_gh_host_env(fake_cli, monkeypatch):
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    assert gh.current_host() == "ghe.example.com"


def test_current_host_defaults_to_github_com_outside_a_repo(fake_cli, monkeypatch):
    monkeypatch.delenv("GH_HOST", raising=False)
    fake_cli.fail(["git", "remote", "get-url", "origin"], stderr="not a git repository")
    assert gh.current_host() == "github.com"


def test_board_pr_details_retries_transient_gateway_errors(fake_cli, monkeypatch):
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    args = ["gh", "api", "--hostname", "github.com", "graphql", "-f", f"query={gh._BOARD_PR_DETAILS_QUERY}", "-f", "ids[]=PR_1"]
    fake_cli.fail(args, stderr="gh: HTTP 502")
    fake_cli.fail(args, stderr="stream error: stream ID 1; CANCEL; received from peer")
    fake_cli.set(args, stdout='{"data": {"nodes": [null]}}')
    assert gh.board_pr_details("github.com", ["PR_1"]) == []


def test_board_pr_details_does_not_retry_other_errors(fake_cli, monkeypatch):
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    args = ["gh", "api", "--hostname", "github.com", "graphql", "-f", f"query={gh._BOARD_PR_DETAILS_QUERY}", "-f", "ids[]=PR_1"]
    fake_cli.fail(args, stderr="gh: Not Found (HTTP 404)")
    fake_cli.set(args, stdout='{"data": {"nodes": []}}')
    with pytest.raises(gh.ZithubError, match="404"):
        gh.board_pr_details("github.com", ["PR_1"])


def test_board_pr_details_skips_bot_comments_and_unescapes_title(fake_cli, monkeypatch):
    monkeypatch.setenv("ZH_BOT_LOGINS", "Github-CI_Acme, other-ci")
    args = ["gh", "api", "--hostname", "github.com", "graphql", "-f", f"query={gh._BOARD_PR_DETAILS_QUERY}", "-f", "ids[]=PR_1"]
    comments = [
        {"createdAt": "t1", "author": {"__typename": "User", "login": "reviewer1"}},
        {"createdAt": "t2", "author": {"__typename": "Bot", "login": "some-app"}},
        {"createdAt": "t3", "author": {"__typename": "User", "login": "snyk-io"}},
        {"createdAt": "t4", "author": {"__typename": "User", "login": "renovate[bot]"}},
        {"createdAt": "t5", "author": {"__typename": "User", "login": "Github-CI_Acme"}},
    ]
    node = {
        "id": "PR_1",
        "number": 1,
        "title": "Schema &amp; marker logic",
        "url": "https://github.com/acme/widgets/pull/1",
        "isDraft": False,
        "reviewDecision": None,
        "createdAt": "t0",
        "updatedAt": "t5",
        "repository": {"nameWithOwner": "acme/widgets"},
        "comments": {"totalCount": 5, "nodes": comments},
        "commits": {"nodes": []},
    }
    fake_cli.set(args, stdout=json.dumps({"data": {"nodes": [node]}}))

    [pr] = gh.board_pr_details("github.com", ["PR_1"])
    assert pr.title == "Schema & marker logic"
    assert pr.last_comment_author == "reviewer1"
    assert pr.last_comment_at == "t1"
    assert pr.comment_count == 5
