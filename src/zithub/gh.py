"""Thin wrappers around the `gh` CLI and its GraphQL API for PR and release
actions gh doesn't already bundle into a single call.

Unlike a status tool, most of these actions delegate straight to `gh`'s own
subprocess (inheriting stdio) so `gh`'s own flags, prompts, and error
messages come through unchanged. The GraphQL calls (review-thread reply/
resolve/unresolve) exist because `gh` has no first-class subcommand for
them.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

_FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}
_PENDING_STATUSES = {"in_progress", "queued", "pending", "waiting", "requested"}


class ZithubError(Exception):
    """Raised when a required CLI is missing or a command fails."""


def _run_result(args: list[str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ZithubError(f"`{args[0]}` not found on PATH") from exc
    if result.returncode != 0:
        raise ZithubError(result.stderr.strip() or f"`{' '.join(args)}` failed")
    return result


def _run(args: list[str]) -> str:
    return _run_result(args).stdout.strip()


def _run_raw(args: list[str]) -> str:
    """Like _run, but only trims the trailing newline — for output (e.g.
    `git status --short`) whose leading whitespace on the first line is
    meaningful and would otherwise be eaten by _run's full .strip()."""
    return _run_result(args).stdout.rstrip("\n")


def _run_json(args: list[str]):
    return json.loads(_run(args))


def run_passthrough(args: list[str]) -> int:
    """Run a `gh` command with stdio inherited, returning its exit code.

    Used for actions where `gh`'s own output/prompts/exit code are exactly
    what should reach the terminal (create, close, comment, review, edit) —
    no reason to re-render what `gh` already prints well.
    """
    try:
        return subprocess.run(args).returncode
    except FileNotFoundError as exc:
        raise ZithubError(f"`{args[0]}` not found on PATH") from exc


@dataclass
class RepoInfo:
    owner: str
    name: str
    name_with_owner: str
    url: str
    default_branch: str


def repo_info() -> RepoInfo:
    data = _run_json(["gh", "repo", "view", "--json", "owner,name,nameWithOwner,url,defaultBranchRef"])
    return RepoInfo(
        owner=data["owner"]["login"],
        name=data["name"],
        name_with_owner=data["nameWithOwner"],
        url=data["url"],
        default_branch=(data.get("defaultBranchRef") or {}).get("name", "?"),
    )


@dataclass
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    details_url: str | None = None


def is_failed(c: CheckRun) -> bool:
    return (c.conclusion or "").lower() in _FAILED_CONCLUSIONS or (c.status or "").lower() == "failure"


def is_pending(c: CheckRun) -> bool:
    return not c.conclusion and (c.status or "").lower() in _PENDING_STATUSES


def is_success(c: CheckRun) -> bool:
    return (c.conclusion or "").lower() == "success" or (c.status or "").lower() == "success"


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    review_decision: str
    head_ref_name: str
    base_ref_name: str
    checks: list[CheckRun]
    body: str = ""


_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName,body"
)


def resolve_pr(ref: str | None = None) -> PullRequest:
    """The PR for `ref` (number, URL, or branch), or the current branch's PR
    if `ref` is None — same resolution `gh pr view` itself does. Raises
    ZithubError if there's no such PR."""
    args = ["gh", "pr", "view", "--json", _PR_VIEW_FIELDS]
    if ref:
        args.insert(3, ref)
    data = _run_json(args)

    checks = [
        CheckRun(
            name=c.get("name") or c.get("context", "?"),
            status=c.get("status", c.get("state", "?")),
            conclusion=c.get("conclusion"),
            details_url=c.get("detailsUrl") or c.get("targetUrl"),
        )
        for c in data.get("statusCheckRollup") or []
    ]
    return PullRequest(
        number=data["number"],
        title=data["title"],
        url=data["url"],
        state=data["state"],
        is_draft=data["isDraft"],
        review_decision=data.get("reviewDecision") or "",
        head_ref_name=data["headRefName"],
        base_ref_name=data["baseRefName"],
        checks=checks,
        body=data.get("body") or "",
    )


@dataclass
class ReviewComment:
    id: str
    author: str
    created_at: str
    body: str
    path: str | None = None
    line: int | None = None


@dataclass
class ReviewThread:
    id: str
    is_resolved: bool
    comments: list[ReviewComment] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        return self.comments[0].path if self.comments else None

    @property
    def line(self) -> int | None:
        return self.comments[0].line if self.comments else None


_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 50) {
            nodes { id body createdAt path line author { login } }
          }
        }
      }
    }
  }
}
"""


def review_threads(owner: str, repo: str, number: int) -> list[ReviewThread]:
    """Every review-comment thread on a PR (resolved and unresolved), each
    with its comments and GraphQL node id — the id is what `resolve`,
    `unresolve`, and `reply` take to act on a specific thread."""
    threads: list[ReviewThread] = []
    after: str | None = None
    while True:
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_REVIEW_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"pr={number}",
        ]
        if after is not None:
            args += ["-F", f"after={after}"]
        data = _run_json(args)
        pr = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        rt = pr.get("reviewThreads") or {}
        for t in rt.get("nodes") or []:
            comments = [
                ReviewComment(
                    id=c["id"],
                    author=(c.get("author") or {}).get("login") or "?",
                    created_at=c.get("createdAt", ""),
                    body=c.get("body", ""),
                    path=c.get("path"),
                    line=c.get("line"),
                )
                for c in (t.get("comments") or {}).get("nodes") or []
            ]
            threads.append(
                ReviewThread(id=t["id"], is_resolved=bool(t.get("isResolved")), comments=comments)
            )
        page_info = rt.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return threads


def resolve_thread(thread_id: str) -> bool:
    """Marks a review thread resolved. Returns the thread's resolved state
    after the call (should always be True on success)."""
    data = _run_json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
            "-F",
            f"id={thread_id}",
        ]
    )
    return bool((((data.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {}).get("isResolved"))


def unresolve_thread(thread_id: str) -> bool:
    data = _run_json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }",
            "-F",
            f"id={thread_id}",
        ]
    )
    return bool((((data.get("data") or {}).get("unresolveReviewThread") or {}).get("thread") or {}).get("isResolved"))


def reply_to_thread(thread_id: str, body: str) -> str:
    """Posts `body` as a reply in an existing review thread. Returns the new
    comment's URL."""
    data = _run_json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body}) { comment { url } } }",
            "-F",
            f"id={thread_id}",
            "-f",
            f"body={body}",
        ]
    )
    comment = (((data.get("data") or {}).get("addPullRequestReviewThreadReply") or {}).get("comment") or {})
    return comment.get("url", "")


# ---------------------------------------------------------------------------
# release preflight + create — ports the github-release skill's preflight.py
# checks (gh auth, target branch, remote sync, CI) into zh so `release
# create` can gate on them directly instead of a separate script + a
# hand-typed `gh release create`.

_DIRTY_IGNORE = {"uv.lock"}


def current_branch() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])


def current_commit_sha() -> str:
    return _run(["git", "rev-parse", "HEAD"])


def fetch_all() -> None:
    _run(["git", "fetch", "origin", "--prune", "--tags"])


def remote_branch_sha(branch: str) -> str | None:
    try:
        return _run(["git", "rev-parse", f"origin/{branch}"])
    except ZithubError:
        return None


def dirty_files() -> list[str]:
    """Uncommitted changes (tracked or untracked) that won't be part of the
    release since the release tag points at HEAD. `uv.lock` is excluded —
    it's routinely rewritten by tooling and not worth flagging every time."""
    lines = _run_raw(["git", "status", "--short"]).splitlines()
    return [line for line in lines if line[3:].strip() not in _DIRTY_IGNORE]


def commits_since(tag: str | None, limit: int = 50) -> list[str]:
    """One-line log of commits since `tag` (or the last `limit` commits if
    there's no previous release yet) — the input a release's notes get
    written from. Best-effort: empty (never raises) if git has no history
    or `tag` doesn't exist locally."""
    args = (
        ["git", "log", f"{tag}..HEAD", "--oneline", "--no-decorate"]
        if tag
        else ["git", "log", f"-{limit}", "--oneline", "--no-decorate"]
    )
    try:
        output = _run(args)
    except ZithubError:
        return []
    return output.splitlines() if output else []


@dataclass
class GhAccount:
    login: str
    active: bool


def list_gh_accounts() -> list[GhAccount]:
    try:
        data = _run_json(["gh", "auth", "status", "--json", "hosts"])
    except ZithubError:
        return []
    return [
        GhAccount(login=a.get("login", ""), active=bool(a.get("active")))
        for a in data.get("hosts", {}).get("github.com", [])
    ]


_REMOTE_OWNER_RE = re.compile(r"[:/]([^/:@]+)/[^/]+/?$")


def _origin_url() -> str | None:
    try:
        return _run(["git", "remote", "get-url", "origin"])
    except ZithubError:
        return None


def _remote_owner() -> str | None:
    """The owner segment of origin's "owner/repo" path — same convention
    wazup uses, so the two tools agree on which account a repo belongs to
    instead of each guessing differently."""
    url = _origin_url()
    if url is None:
        return None
    match = _REMOTE_OWNER_RE.search(url)
    return match.group(1) if match else None


def _login_matches_owner(login: str, owner: str) -> bool:
    if login.lower() == owner.lower():
        return True
    # SSO-linked corporate identities are commonly "personalname_OrgName" —
    # match those against org-owned repos by the part after the underscore.
    if "_" in login:
        return login.rsplit("_", 1)[1].lower() == owner.lower()
    return False


def ensure_gh_account_for_repo() -> str | None:
    """If this repo's owner has a logged-in gh account that isn't active,
    switch to it. Best-effort, like wazup's version of this same check: any
    failure (no remote, gh not installed, no matching account) is a silent
    no-op rather than an error, since this is a convenience, not something
    that should block a release on its own. Returns a human-readable notice
    if a switch happened, else None."""
    owner = _remote_owner()
    if owner is None:
        return None

    accounts = list_gh_accounts()
    match = next((a for a in accounts if _login_matches_owner(a.login, owner)), None)
    if match is None or match.active:
        return None

    try:
        _run(["gh", "auth", "switch", "--hostname", "github.com", "--user", match.login])
    except ZithubError:
        return None
    return f"switched active gh account to {match.login} (owner of {owner}'s repos)"


@dataclass
class ReleaseInfo:
    tag_name: str
    name: str
    published_at: str | None
    is_draft: bool
    is_prerelease: bool


def recent_releases(limit: int = 5) -> list[ReleaseInfo]:
    data = _run_json(
        [
            "gh",
            "release",
            "list",
            "--limit",
            str(limit),
            "--json",
            "tagName,name,publishedAt,isDraft,isPrerelease",
        ]
    )
    return [
        ReleaseInfo(
            tag_name=r["tagName"],
            name=r.get("name") or r["tagName"],
            published_at=r.get("publishedAt"),
            is_draft=r["isDraft"],
            is_prerelease=r["isPrerelease"],
        )
        for r in data
    ]


@dataclass
class WorkflowRun:
    database_id: int
    name: str
    status: str
    conclusion: str | None
    url: str
    head_sha: str | None = None
    created_at: str | None = None


_RUN_LIST_FIELDS = "databaseId,name,status,conclusion,url,headSha,createdAt"


def _workflow_run_from_json(r: dict) -> WorkflowRun:
    return WorkflowRun(
        database_id=r["databaseId"],
        name=r["name"],
        status=r["status"],
        conclusion=r.get("conclusion"),
        url=r["url"],
        head_sha=r.get("headSha"),
        created_at=r.get("createdAt"),
    )


def runs_for_commit(sha: str, limit: int = 100) -> list[WorkflowRun]:
    """Workflow runs whose head commit is exactly `sha` — gates a release on
    CI for the precise commit being tagged, not just "the latest run on this
    branch" (which could predate a since-amended push). Filtered again
    locally since `gh run list --commit` has been seen returning runs from
    other commits on some repos/tokens."""
    data = _run_json(
        ["gh", "run", "list", "--commit", sha, "--limit", str(limit), "--json", _RUN_LIST_FIELDS]
    )
    return [_workflow_run_from_json(r) for r in data if r.get("headSha") == sha]


def latest_runs_for_branch(branch: str, limit: int = 5) -> list[WorkflowRun]:
    data = _run_json(
        [
            "gh",
            "run",
            "list",
            "--branch",
            branch,
            "--limit",
            str(limit),
            "--json",
            _RUN_LIST_FIELDS,
        ]
    )
    return [_workflow_run_from_json(r) for r in data]


def runs_for_event(event: str, limit: int = 10) -> list[WorkflowRun]:
    """Workflow runs triggered by `event` (e.g. "release") — not scoped to a
    branch or commit, since a `release: published` workflow's headBranch is
    the tag, not a real branch, and `gh run list` has no "triggered by this
    exact release" filter. Callers match the run(s) they mean by createdAt
    against a cutoff captured just before the triggering action."""
    data = _run_json(
        ["gh", "run", "list", "--event", event, "--limit", str(limit), "--json", _RUN_LIST_FIELDS]
    )
    return [_workflow_run_from_json(r) for r in data]


def run_failure_log(database_id: int, lines: int = 40) -> str | None:
    """Best-effort tail of a failed run's log — best-effort since a run can
    fail in ways that leave no per-step log (infra failure, cancellation)."""
    try:
        text = _run(["gh", "run", "view", str(database_id), "--log-failed"])
    except ZithubError:
        return None
    if not text:
        return None
    return "\n".join(text.splitlines()[-lines:])


def create_release(
    tag: str,
    notes: str,
    title: str | None = None,
    target: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
) -> str:
    """Creates the GitHub release (and its tag) via `gh release create`.
    Returns the release URL."""
    cmd = ["gh", "release", "create", tag, "--notes", notes]
    if title:
        cmd += ["--title", title]
    if target:
        cmd += ["--target", target]
    if draft:
        cmd.append("--draft")
    if prerelease:
        cmd.append("--prerelease")
    return _run(cmd)
