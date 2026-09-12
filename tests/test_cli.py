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
# release preflight / create

_RUN_LIST_FIELDS = "databaseId,name,status,conclusion,url,headSha,createdAt"


def set_preflight_up_to_ci(fake_cli, branch="main", sha="abc123", dirty=""):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout=branch)
    fake_cli.set(
        ["gh", "auth", "status", "--json", "hosts"],
        stdout=json.dumps({"hosts": {"github.com": [{"login": "vivainio", "active": True}]}}),
    )
    fake_cli.set(["gh", "repo", "view", "--json", "id"], returncode=0)
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
    assert "gh release create <version>" in out


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
    assert "gh release create v1.2.4 --notes" in out and "(patch)" in out
    assert "gh release create v1.3.0 --notes" in out and "(minor)" in out
    assert "gh release create v2.0.0 --notes" in out and "(major)" in out


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


def test_release_bare_sync_mismatch(fake_cli, capsys):
    set_preflight_up_to_ci(fake_cli)
    fake_cli._responses[("git", "rev-parse", "origin/main")] = [("def456", 0, "")]
    rc = run(["release"])
    assert rc == 1
    assert "!= origin/main" in capsys.readouterr().err


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


def test_release_create_runs_preflight_then_creates(fake_cli, monkeypatch, capsys):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    set_preflight_up_to_ci(fake_cli)
    set_ci_success(fake_cli)
    fake_cli.set(
        ["gh", "release", "create", "v1.0.0", "--notes", "first release", "--title", "v1.0.0", "--target", "main"],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    _set_no_publish_workflow(fake_cli)
    rc = run(["release", "create", "v1.0.0", "-n", "first release", "-t", "v1.0.0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "released:" in out


def test_release_create_force_skips_preflight(fake_cli, monkeypatch):
    import zithub.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)
    fake_cli.set(
        ["gh", "release", "create", "v1.0.0", "--notes", "notes", "--title", "v1.0.0"],
        stdout="https://github.com/acme/widgets/releases/tag/v1.0.0",
    )
    _set_no_publish_workflow(fake_cli)
    rc = run(["release", "create", "v1.0.0", "-n", "notes", "--force"])
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
    rc = run(["release", "create", "v1.0.0", "-n", "notes", "--force"])
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
    rc = run(["release", "create", "v1.0.0", "-n", "notes", "--force"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "released:" in out
    assert "release workflow(s) failed" in err


def test_release_create_requires_notes(fake_cli):
    with pytest.raises(SystemExit) as exc:
        run(["release", "create", "v1.0.0", "--force"])
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
    assert 'gh release create v1.2.4 --notes "..." --title v1.2.4' in out


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
    assert 'gh release create v1.3.0 --notes "..." --title v1.3.0' in out
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
