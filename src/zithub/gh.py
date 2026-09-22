"""Thin wrappers around the `gh` CLI and its GraphQL API for PR and release
actions gh doesn't already bundle into a single call.

Unlike a status tool, most of these actions delegate straight to `gh`'s own
subprocess (inheriting stdio) so `gh`'s own flags, prompts, and error
messages come through unchanged. The GraphQL calls (review-thread reply/
resolve/unresolve) exist because `gh` has no first-class subcommand for
them.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}
_PENDING_STATUSES = {"in_progress", "queued", "pending", "waiting", "requested"}


class ZithubError(Exception):
    """Raised when a required CLI is missing or a command fails."""


def _run_result(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=env)
    except FileNotFoundError as exc:
        raise ZithubError(f"`{args[0]}` not found on PATH") from exc
    if result.returncode != 0:
        raise ZithubError(result.stderr.strip() or f"`{' '.join(args)}` failed")
    return result


def _run(args: list[str], env: dict[str, str] | None = None) -> str:
    return _run_result(args, env=env).stdout.strip()


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


def current_repo() -> RepoInfo | None:
    """Like repo_info(), but None instead of raising when cwd isn't a GitHub
    repo — for commands (`issues`) that work either scoped to the current
    repo or cross-repo, depending on whether there is one."""
    try:
        return repo_info()
    except ZithubError:
        return None


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
    head_sha: str = ""


_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName,body,"
    "headRefOid"
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
        head_sha=data.get("headRefOid") or "",
    )


def current_pr(ref: str | None = None) -> PullRequest | None:
    """Like resolve_pr(), but None instead of raising when there's no such
    PR — for status commands that fall back to CI-runs-for-a-branch instead
    of erroring out."""
    try:
        return resolve_pr(ref)
    except ZithubError:
        return None


_BOARD_PRS_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        reviewDecision
        createdAt
        updatedAt
        repository { nameWithOwner }
        comments(last: 1) {
          totalCount
          nodes { createdAt author { login } }
        }
        commits(last: 1) {
          nodes { commit { statusCheckRollup { state } } }
        }
      }
    }
  }
}
"""

_ROLLUP_STATE_TO_CI_STATE = {
    "SUCCESS": "success",
    "PENDING": "pending",
    "EXPECTED": "pending",
    "ERROR": "failed",
    "FAILURE": "failed",
}


def board_prs() -> list[PullRequestSummary]:
    """Every open PR authored by you, with the per-PR detail `zh board`
    needs (review decision, aggregate CI state, last comment) — one
    GraphQL `search` query, paginated 100 at a time, instead of a search
    call plus a separate `gh pr view` round trip per PR."""
    results: list[PullRequestSummary] = []
    after: str | None = None
    while True:
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_BOARD_PRS_QUERY}",
            "-F",
            "q=is:pr is:open author:@me",
        ]
        if after:
            args += ["-F", f"after={after}"]
        data = _run_json(args)["data"]["search"]
        for node in data["nodes"]:
            comments = node["comments"]["nodes"]
            last_comment = comments[-1] if comments else None
            commit_nodes = node["commits"]["nodes"]
            rollup = (commit_nodes[0]["commit"].get("statusCheckRollup") if commit_nodes else None) or {}
            results.append(
                PullRequestSummary(
                    number=node["number"],
                    title=node["title"],
                    url=node["url"],
                    state="OPEN",
                    is_draft=node["isDraft"],
                    updated_at=node["updatedAt"],
                    repo=node["repository"]["nameWithOwner"],
                    review_decision=node.get("reviewDecision") or "",
                    ci_state=_ROLLUP_STATE_TO_CI_STATE.get(rollup.get("state"), "none"),
                    created_at=node["createdAt"],
                    comment_count=node["comments"]["totalCount"],
                    last_comment_at=(last_comment or {}).get("createdAt"),
                    last_comment_author=((last_comment or {}).get("author") or {}).get("login"),
                )
            )
        page_info = data["pageInfo"]
        if not page_info["hasNextPage"]:
            return results
        after = page_info["endCursor"]


@dataclass
class ReviewComment:
    id: str
    author: str
    created_at: str
    body: str
    path: str | None = None
    line: int | None = None
    diff_hunk: str = ""


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
            pageInfo { hasNextPage endCursor }
            nodes { id body createdAt path line diffHunk author { login } }
          }
        }
      }
    }
  }
}
"""

# A thread's own comments are a connection nested inside reviewThreads, so
# GraphQL has no way to page it directly from the PR query above once a
# thread has more than one page of comments — re-fetch that thread by its
# own node id instead.
_THREAD_COMMENTS_QUERY = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id body createdAt path line diffHunk author { login } }
      }
    }
  }
}
"""


def _parse_review_comment(c: dict) -> ReviewComment:
    return ReviewComment(
        id=c["id"],
        author=(c.get("author") or {}).get("login") or "?",
        created_at=c.get("createdAt", ""),
        body=c.get("body", ""),
        path=c.get("path"),
        line=c.get("line"),
        diff_hunk=c.get("diffHunk") or "",
    )


def _remaining_thread_comments(thread_id: str, after: str | None) -> list[ReviewComment]:
    """Comments past the first page of one review thread."""
    comments: list[ReviewComment] = []
    while True:
        data = _run_json(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_THREAD_COMMENTS_QUERY}",
                "-F",
                f"id={thread_id}",
                "-F",
                f"after={after}",
            ]
        )
        page = ((data.get("data") or {}).get("node") or {}).get("comments") or {}
        comments += [_parse_review_comment(c) for c in page.get("nodes") or []]
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return comments
        after = page_info.get("endCursor")


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
            comment_page = t.get("comments") or {}
            comments = [_parse_review_comment(c) for c in comment_page.get("nodes") or []]
            comment_page_info = comment_page.get("pageInfo") or {}
            if comment_page_info.get("hasNextPage"):
                comments += _remaining_thread_comments(t["id"], comment_page_info.get("endCursor"))
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


def worktree_root() -> str | None:
    try:
        return _run(["git", "rev-parse", "--show-toplevel"])
    except ZithubError:
        return None


def remote_name_with_owner() -> str | None:
    """"owner/repo" parsed from origin's remote URL, entirely locally — no
    `gh` call, so it stays fast and works even without `gh` installed or
    authenticated. Used to key the local-checkout registry (`zh repos`)
    instead of a `gh repo view` round trip."""
    url = _origin_url()
    if url is None:
        return None
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    parts = re.split(r"[/:]", url)
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        return None
    return f"{parts[-2]}/{parts[-1]}"


_PR_URL_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+)/pull/\d+")


def repo_for_ref(ref: str | None) -> str | None:
    """"owner/repo" for a PR ref, without needing a `gh` call. `zh pr check`
    is typically passed a full PR URL and run from wherever the agent
    already is, not necessarily a checkout of that repo — so a URL's
    owner/repo is parsed directly from it rather than assumed to be the
    current directory's. Falls back to the current directory's remote for
    a bare number/branch ref (or no ref at all), which has no repo of its
    own to parse."""
    if ref:
        match = _PR_URL_RE.search(ref)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return remote_name_with_owner()


def fetch_all() -> None:
    _run(["git", "fetch", "origin", "--prune", "--tags"])


def remote_branch_sha(branch: str) -> str | None:
    try:
        return _run(["git", "rev-parse", f"origin/{branch}"])
    except ZithubError:
        return None


def dirty_files(path: str | None = None) -> list[str]:
    """Uncommitted changes (tracked or untracked) in `path` (default: cwd) —
    for a release, ones that won't be part of it since the release tag
    points at HEAD; for another checkout (e.g. one `pr check` found in the
    registry), a heads-up before an agent switches branches or pulls there.
    `uv.lock` is excluded — it's routinely rewritten by tooling and not
    worth flagging every time."""
    cmd = ["git", *(["-C", path] if path else []), "status", "--short"]
    lines = _run_raw(cmd).splitlines()
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


# ---------------------------------------------------------------------------
# status/ci — read-side reporting ported from wazup, so zh doesn't require it
# installed alongside for a basic "what's up with this repo" view.

def is_linked_worktree() -> bool:
    """True if the current checkout is a linked worktree (`git worktree
    add`), not a repo's main checkout — the two share a common git dir but
    a linked worktree gets its own private one under
    `<common>/worktrees/<name>`, so the two paths differ only there."""
    try:
        git_dir, common_dir = _run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"]
        ).splitlines()
    except ZithubError:
        return False
    return os.path.realpath(git_dir) != os.path.realpath(common_dir)


def commits_ahead_of(ref: str) -> int | None:
    """How many commits HEAD has that `ref` doesn't. None if `ref` doesn't
    exist locally (e.g. no `origin/<branch>` without a fetch yet)."""
    try:
        return int(_run(["git", "rev-list", "--count", f"{ref}..HEAD"]))
    except ZithubError:
        return None


def ahead_behind(ref: str) -> tuple[int, int] | None:
    """(ahead, behind) commit counts between HEAD and `ref` — how many
    commits HEAD has that `ref` doesn't, and vice versa. None if `ref`
    doesn't exist locally. Used to tell whether a sync mismatch needs a
    push (ahead only), a pull (behind only), or a real reconcile (both)."""
    try:
        out = _run(["git", "rev-list", "--left-right", "--count", f"HEAD...{ref}"])
    except ZithubError:
        return None
    ahead, behind = out.split()
    return int(ahead), int(behind)


@dataclass
class ChangedFile:
    status: str  # single-letter: M, A, D, R, C, U
    path: str


@dataclass
class LocalStatus:
    ahead: int | None
    behind: int | None
    changed_files: list[ChangedFile]
    untracked_count: int


def start_background_fetch() -> threading.Thread:
    """Kick off `git fetch` on its own thread so `local_status()` can report
    ahead/behind against origin without a stale remote-tracking ref. Runs
    concurrently with the gh API calls the caller is about to make, so its
    latency overlaps with work that's already happening rather than adding
    to it — join the returned thread before calling `local_status()`.
    Failure (offline, no fetch access, etc.) is a silent no-op: this is
    best-effort freshness, not a hard requirement. Runs with prompts
    disabled (GIT_TERMINAL_PROMPT=0, batch-mode SSH) so a repo with no
    cached credentials fails fast instead of blocking forever on a prompt
    nothing can answer — there's no TTY attached to a background thread we
    `join()` on before printing."""

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes",
    }

    def fetch() -> None:
        try:
            _run(["git", "fetch"], env=env)
        except ZithubError:
            pass

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()
    return thread


def local_status() -> LocalStatus:
    """Unpushed/behind commit counts (None if no upstream), tracked changes
    (staged or unstaged), and a separate untracked-file count. Untracked
    files are kept out of `changed_files` since they're often noise (scratch
    files, build output) rather than something you forgot to commit."""
    try:
        data = _run(["git", "status", "--porcelain=v2", "--branch"])
    except ZithubError:
        return LocalStatus(ahead=None, behind=None, changed_files=[], untracked_count=0)

    ahead = None
    behind = None
    changed: list[ChangedFile] = []
    untracked_count = 0
    for line in data.splitlines():
        if line.startswith("# branch.ab "):
            _, _, ahead_str, behind_str = line.split()
            ahead = int(ahead_str.lstrip("+"))
            behind = int(behind_str.lstrip("-"))
        elif line.startswith("1 "):
            xy, path = line.split(" ", 8)[1], line.split(" ", 8)[8]
            changed.append(ChangedFile(status=xy[0] if xy[0] != "." else xy[1], path=path))
        elif line.startswith("2 "):
            xy, rest = line.split(" ", 9)[1], line.split(" ", 9)[9]
            path = rest.split("\t", 1)[0]
            changed.append(ChangedFile(status=xy[0] if xy[0] != "." else xy[1], path=path))
        elif line.startswith("u "):
            path = line.split(" ", 10)[10]
            changed.append(ChangedFile(status="U", path=path))
        elif line.startswith("? "):
            untracked_count += 1

    return LocalStatus(
        ahead=ahead, behind=behind, changed_files=changed, untracked_count=untracked_count
    )


@dataclass
class BranchInfo:
    name: str
    relative_date: str


def local_branches_by_recency(exclude: set[str] | None = None) -> list[BranchInfo]:
    """All local branches, most recently committed to first."""
    exclude = exclude or set()
    try:
        data = _run(
            [
                "git",
                "for-each-ref",
                "refs/heads/",
                "--sort=-committerdate",
                "--format=%(refname:short)\t%(committerdate:relative)",
            ]
        )
    except ZithubError:
        return []

    branches = []
    for line in data.splitlines():
        name, _, relative_date = line.partition("\t")
        if name and name not in exclude:
            branches.append(BranchInfo(name=name, relative_date=relative_date))
    return branches


def is_orphaned_worktree_branch_name(name: str) -> bool:
    """True for a `worktree/`-prefixed branch name (herdr's convention for
    throwaway worktree branches). Only meaningful for branches that aren't
    currently checked out in a live worktree (those are handled separately)
    — a match here means the worktree was removed but the branch it
    generated was left behind."""
    return name.startswith("worktree/")


def worktree_branches() -> dict[str, str]:
    """Map local branch name -> worktree path, for branches checked out in a
    worktree (including the current one)."""
    try:
        data = _run(["git", "worktree", "list", "--porcelain"])
    except ZithubError:
        return {}

    result: dict[str, str] = {}
    path: str | None = None
    for line in data.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/") and path is not None:
            result[line[len("branch refs/heads/") :]] = path
    return result


@dataclass
class WorktreeInfo:
    branch: str
    path: str
    relative_date: str
    unmerged: int | None
    dirty: bool


def worktree_info(exclude_branch: str, default_ref: str | None = None) -> list[WorktreeInfo]:
    """Other worktrees' branches, most recently committed to first. If
    `default_ref` is given (e.g. "origin/main"), each entry also reports how
    many commits it has that aren't reachable from that ref — 0 means it's
    fully merged and the worktree is safe to prune. None (rather than a
    count) means it couldn't be determined, e.g. `default_ref` doesn't exist
    locally yet."""
    entries: list[tuple[int, WorktreeInfo]] = []
    for branch, path in worktree_branches().items():
        if branch == exclude_branch:
            continue
        try:
            data = _run(["git", "log", "-1", "--format=%ct\t%cr", branch])
        except ZithubError:
            continue
        timestamp, _, relative_date = data.partition("\t")

        unmerged = None
        if default_ref is not None:
            try:
                unmerged = int(_run(["git", "rev-list", "--count", f"{default_ref}..{branch}"]))
            except ZithubError:
                pass

        try:
            dirty = bool(_run(["git", "-C", path, "status", "--porcelain"]))
        except ZithubError:
            dirty = False

        entries.append(
            (
                int(timestamp),
                WorktreeInfo(
                    branch=branch,
                    path=path,
                    relative_date=relative_date,
                    unmerged=unmerged,
                    dirty=dirty,
                ),
            )
        )
    entries.sort(key=lambda e: e[0], reverse=True)
    return [w for _, w in entries]


@dataclass
class BranchPr:
    number: int
    url: str
    state: str
    is_draft: bool


def open_prs_by_branch() -> dict[str, BranchPr]:
    """One call to map every open PR's head branch to its PR, for cheap lookup."""
    try:
        data = _run_json(
            [
                "gh",
                "pr",
                "list",
                "--json",
                "number,url,state,isDraft,headRefName",
                "--limit",
                "100",
            ]
        )
    except ZithubError:
        return {}
    return {
        p["headRefName"]: BranchPr(
            number=p["number"], url=p["url"], state=p["state"], is_draft=p["isDraft"]
        )
        for p in data
    }


_RUNNING_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}
_RECENT_OTHER_RUN_WINDOW_SECONDS = 60 * 60


def _parse_utc_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_age_minutes(created_at: str | None) -> int | None:
    """Minutes since a workflow run started — used to show "Nm ago" instead
    of a redundant link on a run that already succeeded."""
    created = _parse_utc_iso(created_at)
    if created is None:
        return None
    return int((datetime.now(timezone.utc) - created).total_seconds() // 60)


def recent_other_runs(limit: int = 20) -> list[WorkflowRun]:
    """Workflow runs anywhere in the repo that are still active, or
    completed within the last hour — not scoped to a branch, since a
    release-triggered run's headBranch is the tag, not the branch whose
    push triggered the CI run above, so `--branch` filtering misses it
    entirely. The recency window keeps this from resurfacing every
    completed run in the repo's history on every invocation."""
    data = _run_json(["gh", "run", "list", "--limit", str(limit), "--json", _RUN_LIST_FIELDS])
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_RECENT_OTHER_RUN_WINDOW_SECONDS)
    runs = []
    for r in data:
        if (r.get("status") or "").lower() not in _RUNNING_STATUSES:
            created_at = _parse_utc_iso(r.get("createdAt"))
            if created_at is None or created_at < cutoff:
                continue
        runs.append(_workflow_run_from_json(r))
    return runs


@dataclass
class PullRequestSummary:
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    updated_at: str
    repo: str | None = None
    # populated only by board_prs(), for `zh board` — the staleness
    # signals that a plain `gh pr list`/`gh search prs` doesn't return.
    review_decision: str = ""
    ci_state: str = ""
    created_at: str = ""
    comment_count: int = 0
    last_comment_at: str | None = None
    last_comment_author: str | None = None


def my_open_prs_in_repo() -> list[PullRequestSummary]:
    data = _run_json(
        ["gh", "pr", "list", "--author", "@me", "--json", "number,title,url,state,isDraft,updatedAt"]
    )
    return [
        PullRequestSummary(
            number=p["number"],
            title=p["title"],
            url=p["url"],
            state=p["state"],
            is_draft=p["isDraft"],
            updated_at=p["updatedAt"],
        )
        for p in data
    ]


_SEARCH_PR_FIELDS = "number,title,url,state,isDraft,updatedAt,repository"
# GitHub search caps a single query's results; 200 comfortably covers a
# personal open-PR count without needing pagination.
_SEARCH_PR_LIMIT = "200"


def _pr_summary(p: dict) -> PullRequestSummary:
    return PullRequestSummary(
        number=p["number"],
        title=p["title"],
        url=p["url"],
        state=p["state"],
        is_draft=p["isDraft"],
        updated_at=p["updatedAt"],
        repo=p.get("repository", {}).get("nameWithOwner"),
    )


def my_recent_prs(since: str) -> list[PullRequestSummary]:
    # Two separate queries, not one `--updated >=since` search: a single
    # query sorted by "updated" across all states/repos gets dominated by
    # merge/close churn in busy repos, silently pushing older-but-still-open
    # PRs in quieter repos past the (default 30, here 200) result cap. Open
    # PRs are always relevant regardless of last-touched date, so they're
    # fetched unconditionally; the date filter only trims the closed/merged
    # side, which is shown as recent-activity context.
    open_data = _run_json(
        [
            "gh",
            "search",
            "prs",
            "--author",
            "@me",
            "--state",
            "open",
            "--limit",
            _SEARCH_PR_LIMIT,
            "--json",
            _SEARCH_PR_FIELDS,
        ]
    )
    closed_data = _run_json(
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
            _SEARCH_PR_LIMIT,
            "--json",
            _SEARCH_PR_FIELDS,
        ]
    )
    return [_pr_summary(p) for p in open_data] + [_pr_summary(p) for p in closed_data]


def prs_awaiting_my_review(since: str) -> list[PullRequestSummary]:
    data = _run_json(
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
        ]
    )
    return [
        PullRequestSummary(
            number=p["number"],
            title=p["title"],
            url=p["url"],
            state=p["state"],
            is_draft=p["isDraft"],
            updated_at=p["updatedAt"],
            repo=p.get("repository", {}).get("nameWithOwner"),
        )
        for p in data
    ]


@dataclass
class IssueSummary:
    number: int
    title: str
    url: str
    state: str
    updated_at: str
    repo: str | None = None


def my_open_issues_in_repo() -> list[IssueSummary]:
    data = _run_json(
        ["gh", "issue", "list", "--assignee", "@me", "--json", "number,title,url,state,updatedAt"]
    )
    return [
        IssueSummary(
            number=i["number"],
            title=i["title"],
            url=i["url"],
            state=i["state"],
            updated_at=i["updatedAt"],
        )
        for i in data
    ]


def my_open_issues(since: str) -> list[IssueSummary]:
    data = _run_json(
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
        ]
    )
    return [
        IssueSummary(
            number=i["number"],
            title=i["title"],
            url=i["url"],
            state=i["state"],
            updated_at=i["updatedAt"],
            repo=i.get("repository", {}).get("nameWithOwner"),
        )
        for i in data
    ]


# ---------------------------------------------------------------------------
# failure log tails for `--why` — separate from run_failure_log() above,
# which fetches by a known databaseId (release/merge preflight always have
# one already). These take a PR check's detailsUrl instead (a job-level or
# bare run-level Actions URL) and cache the fetched log to disk, since PR
# checks get re-fetched on every repo visit rather than once per release cut.

_RUN_JOB_URL_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
_RUN_URL_RE = re.compile(r"/actions/runs/(\d+)")
_JOB_FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
_LOG_LINE_RE = re.compile(r"^[^\t]*\t[^\t]*\t\S+Z\s?")
_LOG_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _log_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "zithub", "logs")


def _log_cache_path(run_id: str, job_id: str) -> str:
    return os.path.join(_log_cache_dir(), f"{run_id}-{job_id}.log")


def _prune_log_cache() -> None:
    """Drop cached logs older than a week, so the cache doesn't grow
    forever. Only runs on a cache miss (not every read), since that's
    already the slow path."""
    cache_dir = _log_cache_dir()
    cutoff = time.time() - _LOG_CACHE_MAX_AGE_SECONDS
    try:
        entries = os.scandir(cache_dir)
    except OSError:
        return
    with entries:
        for entry in entries:
            try:
                if entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
            except OSError:
                pass


def _job_log_tail(run_id: str, job_id: str, lines: int) -> str | None:
    """A job's log is only fetched once we already know it's in a terminal
    (failed) state, so its content is immutable — safe to cache to disk
    forever, keyed by run/job id, instead of re-fetching on every --why."""
    cache_path = _log_cache_path(run_id, job_id)
    try:
        with open(cache_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        try:
            raw = _run(["gh", "run", "view", run_id, "--job", job_id, "--log-failed"])
        except ZithubError:
            return None
        try:
            os.makedirs(_log_cache_dir(), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(raw)
            _prune_log_cache()
        except OSError:
            pass

    cleaned = [_LOG_LINE_RE.sub("", line) for line in raw.splitlines() if line.strip()]
    if not cleaned:
        return None

    error_idx = next(
        (i for i in range(len(cleaned) - 1, -1, -1) if "##[error]" in cleaned[i]),
        len(cleaned) - 1,
    )
    start = max(0, error_idx - lines + 1)
    return "\n".join(cleaned[start : error_idx + 1])


@functools.lru_cache(maxsize=None)
def _run_jobs(run_id: str) -> tuple[dict, ...]:
    """Cached: a PR's checks are usually jobs of the same workflow run, so
    without this, listing N failed checks from one run would re-fetch the
    identical job list N times."""
    try:
        return tuple(_run_json(["gh", "run", "view", run_id, "--json", "jobs"])["jobs"])
    except ZithubError:
        return ()


def _find_failed_job(run_id: str) -> dict | None:
    return next(
        (
            j
            for j in _run_jobs(run_id)
            if (j.get("conclusion") or "").lower() in _JOB_FAILED_CONCLUSIONS
        ),
        None,
    )


def failure_log_tail(details_url: str | None, lines: int = 25) -> str | None:
    """Best-effort tail of a failed GitHub Actions job's log, ending at the
    error. Accepts either a job-level detailsUrl (from a PR check) or a bare
    run-level URL (from `gh run list`, which has no job id) — for the
    latter, the failed job is resolved via `gh run view --json jobs`."""
    if not details_url:
        return None

    job_match = _RUN_JOB_URL_RE.search(details_url)
    if job_match:
        run_id, job_id = job_match.groups()
    else:
        run_match = _RUN_URL_RE.search(details_url)
        if not run_match:
            return None
        run_id = run_match.group(1)
        failed_job = _find_failed_job(run_id)
        if failed_job is None:
            return None
        job_id = str(failed_job["databaseId"])

    return _job_log_tail(run_id, job_id, lines)


def _duration(started: str | None, completed: str | None) -> str | None:
    if not started or not completed:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{int((end - start).total_seconds())}s"


def _failed_step_summaries(job: dict) -> list[str]:
    summaries = []
    for s in job.get("steps") or []:
        if (s.get("conclusion") or "").lower() not in _JOB_FAILED_CONCLUSIONS:
            continue
        dur = _duration(s.get("startedAt"), s.get("completedAt"))
        summaries.append(f"{s['name']} ({dur})" if dur else s["name"])
    return summaries


def failed_steps_summary(details_url: str | None) -> str | None:
    """One-line summary of what failed — job/step metadata only, no log
    fetch, so it's cheap enough to show by default (not gated behind --why).
    Accepts a job-level or bare run-level Actions URL, same as
    failure_log_tail.

    Normally this names the failed step(s), each with how long it ran
    before failing. If no individual step is marked failed (the job died
    some other way - cancelled, infra failure), falls back to how far the
    job got: how many steps completed and how long it ran - still useful
    context, and still free of any extra fetch beyond the job/step list
    already retrieved."""
    if not details_url:
        return None

    job_match = _RUN_JOB_URL_RE.search(details_url)
    if job_match:
        run_id, job_id = job_match.groups()
        job = next((j for j in _run_jobs(run_id) if str(j.get("databaseId")) == job_id), None)
    else:
        run_match = _RUN_URL_RE.search(details_url)
        if not run_match:
            return None
        job = _find_failed_job(run_match.group(1))

    if job is None:
        return None

    steps = _failed_step_summaries(job)
    if steps:
        return f"{job['name']}: {', '.join(steps)}"

    all_steps = job.get("steps") or []
    completed = sum(1 for s in all_steps if s.get("status") == "completed")
    dur = _duration(job.get("startedAt"), job.get("completedAt"))
    if not all_steps:
        return job["name"]
    progress = f"stopped after {completed}/{len(all_steps)} steps"
    return f"{job['name']} ({progress}, {dur})" if dur else f"{job['name']} ({progress})"
