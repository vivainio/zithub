"""Thin wrappers around the `gh` CLI and its GraphQL API for PR write actions.

Unlike a status tool, most of these actions delegate straight to `gh`'s own
subprocess (inheriting stdio) so `gh`'s own flags, prompts, and error
messages come through unchanged. The GraphQL calls (review-thread reply/
resolve/unresolve) exist because `gh` has no first-class subcommand for
them.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

_FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}
_PENDING_STATUSES = {"in_progress", "queued", "pending", "waiting", "requested"}


class ZithubError(Exception):
    """Raised when a required CLI is missing or a command fails."""


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ZithubError(f"`{args[0]}` not found on PATH") from exc
    if result.returncode != 0:
        raise ZithubError(result.stderr.strip() or f"`{' '.join(args)}` failed")
    return result.stdout.strip()


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


_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,statusCheckRollup,headRefName,baseRefName"
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
