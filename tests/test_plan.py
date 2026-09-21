"""zh plan: scaffold rendering, plan-file parsing, and `--apply` end to end."""

from __future__ import annotations

import json

import pytest

from zithub import gh, plan
from zithub.cli import build_parser

SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
_REPO_FIELDS = "owner,name,nameWithOwner,url,defaultBranchRef"
_RESOLVE = "query=mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
_REPLY = (
    "query=mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply("
    "input: {pullRequestReviewThreadId: $id, body: $body}) { comment { url } } }"
)


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def _pr(**overrides) -> str:
    data = {
        "number": 7,
        "title": "Add widget",
        "url": "https://github.com/acme/widgets/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": "",
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": SHA,
        "statusCheckRollup": [{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    data.update(overrides)
    return json.dumps(data)


def _threads(*threads: tuple[str, bool]) -> str:
    nodes = [
        {
            "id": tid,
            "isResolved": resolved,
            "comments": {
                "nodes": [
                    {
                        "id": f"C_{tid}",
                        "body": "why drop the fallback?",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "path": "a.xsl",
                        "line": 42,
                        "diffHunk": "@@ -1,2 +1,2 @@\n-old\n+new",
                        "author": {"login": "alice"},
                    }
                ]
            },
        }
        for tid, resolved in threads
    ]
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
                    }
                }
            }
        }
    )


def _threads_cmd() -> list[str]:
    return [
        "gh", "api", "graphql", "-f", f"query={gh._REVIEW_THREADS_QUERY}",
        "-F", "owner=acme", "-F", "repo=widgets", "-F", "pr=7",
    ]  # fmt: skip


def _stub_pr(fake_cli, threads: str, ref: str | None = "7", pr: str | None = None) -> None:
    fake_cli.set(["gh", "repo", "view", "--json", _REPO_FIELDS], stdout=json.dumps(
        {
            "owner": {"login": "acme"},
            "name": "widgets",
            "nameWithOwner": "acme/widgets",
            "url": "https://github.com/acme/widgets",
            "defaultBranchRef": {"name": "main"},
        }
    ))  # fmt: skip
    view = ["gh", "pr", "view", *([ref] if ref else []), "--json", gh._PR_VIEW_FIELDS]
    fake_cli.set(view, stdout=pr or _pr())
    fake_cli.set(_threads_cmd(), stdout=threads)


HEADER = f"@pr acme/widgets#7 head={SHA}\n"


# ---------------------------------------------------------------------------
# parse

def test_parse_all_verbs():
    p = plan.parse(
        HEADER
        + "# a comment\n"
        + "reply T_1\n  Fixed in abc.\n\n  Second paragraph.\n"
        + "resolve T_1 T_2\n"
        + "unresolve T_3\n"
        + "ship rebase\n"
    )
    assert (p.repo, p.number, p.head) == ("acme/widgets", 7, SHA)
    assert [a.kind for a in p.actions] == ["reply", "resolve", "unresolve", "ship"]
    assert p.actions[0].body == "Fixed in abc.\n\nSecond paragraph."
    assert p.actions[1].args == ["T_1", "T_2"]
    assert p.actions[3].args == ["rebase"]


def test_parse_merge_defaults_to_squash():
    assert plan.parse(HEADER + "merge\n").actions[0].args == ["squash"]


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("resolve T_1\n", "before the @pr header"),
        (HEADER + HEADER, "more than one"),
        ("@pr nonsense\n", "malformed header"),
        (HEADER + "frobnicate T_1\n", "unknown action"),
        (HEADER + "reply T_1\n", "no body"),
        (HEADER + "reply T_1\n  <text>\n", "no body"),
        (HEADER + "resolve\n", "at least one"),
        (HEADER + "merge bogus\n", "optional method"),
        (HEADER + "merge\nresolve T_1\n", "nothing may follow"),
        (HEADER + "  stray indent\n", "indented text"),
        ("# only comments\n", "missing"),
    ],
)
def test_parse_errors(text, fragment):
    with pytest.raises(plan.PlanError, match=fragment):
        plan.parse(text)


def test_untouched_scaffold_parses_to_no_actions():
    pr = gh.PullRequest(
        number=7, title="Add widget", url="u", state="OPEN", is_draft=False, review_decision="",
        head_ref_name="f", base_ref_name="main", checks=[], head_sha=SHA,
    )  # fmt: skip
    thread = gh.ReviewThread(
        id="T_1",
        is_resolved=False,
        comments=[gh.ReviewComment("C", "alice", "2026-01-01", "why?\nreally", "a.xsl", 42, "@@\n-old\n+new")],
    )
    text = plan.render("acme/widgets", pr, [thread])
    assert "## thread T_1  a.xsl:42  (unresolved)" in text
    assert "#   > alice" not in text and "#   > why?" in text and "#   | +new" in text
    assert plan.parse(text).actions == []


def test_uncommenting_scaffold_lines_yields_actions():
    pr = gh.PullRequest(
        number=7, title="t", url="u", state="OPEN", is_draft=False, review_decision="",
        head_ref_name="f", base_ref_name="main", checks=[], head_sha=SHA,
    )  # fmt: skip
    thread = gh.ReviewThread("T_1", False, [gh.ReviewComment("C", "alice", "d", "why?", "a", 1)])
    lines = plan.render("acme/widgets", pr, [thread]).splitlines()
    edited = []
    for line in lines:
        if line.startswith("# reply T_1"):
            line = line[2:]
        elif line == "#   <text>":
            line = "  Fixed."
        elif line.startswith("# resolve T_1"):
            line = line[2:]
        edited.append(line)
    actions = plan.parse("\n".join(edited)).actions
    assert [(a.kind, a.body) for a in actions] == [("reply", "Fixed."), ("resolve", "")]


# ---------------------------------------------------------------------------
# zh plan (generate)

def test_plan_generate_prints_scaffold(fake_cli, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False), ("T_2", True)), ref=None)
    assert run(["plan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"@pr acme/widgets#7 head={SHA}")
    assert "T_1" in out and "T_2" not in out


def test_plan_generate_refuses_to_overwrite(fake_cli, tmp_path, capsys):
    target = tmp_path / "plan.md"
    target.write_text("my edits")
    _stub_pr(fake_cli, _threads(), ref=None)
    assert run(["plan", "-o", str(target)]) == 1
    assert target.read_text() == "my edits"
    assert "--force" in capsys.readouterr().err


def test_plan_generate_rejects_branch_names(fake_cli, capsys):
    assert run(["plan", "feature-x"]) == 1
    assert "branch names aren't supported" in capsys.readouterr().err
    assert fake_cli.calls == []


def test_plan_generate_accepts_pr_url(fake_cli, capsys):
    url = "https://github.com/acme/widgets/pull/7"
    _stub_pr(fake_cli, _threads(("T_1", False)), ref=url)
    assert run(["plan", url]) == 0


# ---------------------------------------------------------------------------
# zh plan --apply

def _write(tmp_path, body: str) -> str:
    f = tmp_path / "plan.md"
    f.write_text(body)
    return str(f)


def test_apply_dry_run_changes_nothing(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False)))
    f = _write(tmp_path, HEADER + "reply T_1\n  Fixed.\nresolve T_1\n")
    assert run(["plan", "--apply", f, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1. reply to T_1" in out and "2. resolve T_1" in out and "dry run" in out
    assert not any("mutation" in " ".join(c) for c in fake_cli.calls)


def test_apply_runs_actions_in_order(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False)))
    fake_cli.set(
        ["gh", "api", "graphql", "-f", _REPLY, "-F", "id=T_1", "-f", "body=Fixed."],
        stdout=json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"url": "https://x/c"}}}}),
    )
    fake_cli.set(
        ["gh", "api", "graphql", "-f", _RESOLVE, "-F", "id=T_1"],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
    )
    f = _write(tmp_path, HEADER + "reply T_1\n  Fixed.\nresolve T_1\n")
    assert run(["plan", "--apply", f]) == 0
    mutations = [c for c in fake_cli.calls if any("mutation" in part for part in c)]
    assert [("Reply" in m[4]) for m in mutations] == [True, False]


def test_apply_refuses_stale_head(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False)), pr=_pr(headRefOid="f" * 40))
    f = _write(tmp_path, HEADER + "resolve T_1\n")
    assert run(["plan", "--apply", f]) == 1
    err = capsys.readouterr().err
    assert "stale plan" in err and "nothing was applied" in err
    assert not any("mutation" in " ".join(c) for c in fake_cli.calls)


def test_apply_rejects_unknown_thread_before_running_anything(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False)))
    f = _write(tmp_path, HEADER + "resolve T_1\nresolve T_nope\n")
    assert run(["plan", "--apply", f]) == 1
    assert "no such thread" in capsys.readouterr().err
    assert not any("mutation" in " ".join(c) for c in fake_cli.calls)


def test_apply_merge_blocked_by_thread_left_unresolved(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False), ("T_2", False)))
    f = _write(tmp_path, HEADER + "resolve T_1\nmerge\n")
    assert run(["plan", "--apply", f, "--dry-run"]) == 1
    assert "1 thread(s) still unresolved" in capsys.readouterr().err


def test_apply_merge_ok_when_plan_resolves_every_thread(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False)))
    f = _write(tmp_path, HEADER + "resolve T_1\nship squash\n")
    assert run(["plan", "--apply", f, "--dry-run"]) == 0
    assert "ship (squash)" in capsys.readouterr().out


def test_apply_reports_partial_progress_on_failure(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads(("T_1", False), ("T_2", False)))
    fake_cli.set(
        ["gh", "api", "graphql", "-f", _RESOLVE, "-F", "id=T_1"],
        stdout=json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
    )
    fake_cli.fail(["gh", "api", "graphql", "-f", _RESOLVE, "-F", "id=T_2"], stderr="rate limited")
    f = _write(tmp_path, HEADER + "resolve T_1\nresolve T_2\n")
    assert run(["plan", "--apply", f]) == 1
    err = capsys.readouterr().err
    assert "step 2" in err and "applied 1 of 2" in err and "rate limited" in err


def test_apply_wrong_repo(fake_cli, tmp_path, capsys):
    _stub_pr(fake_cli, _threads())
    f = _write(tmp_path, f"@pr other/repo#7 head={SHA}\nresolve T_1\n")
    assert run(["plan", "--apply", f]) == 1
    assert "other/repo" in capsys.readouterr().err
