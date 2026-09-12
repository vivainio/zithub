"""zh: batched `gh` PR actions for AI agents — merge preflight, review-thread
reply/resolve, and the rest of the PR write-side, each as one call instead
of several chained `gh`/API calls."""

from __future__ import annotations

import argparse
import sys
import time

from . import gh


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(text: str) -> str:
    return _color(text, "32")


def _red(text: str) -> str:
    return _color(text, "31")


def _yellow(text: str) -> str:
    return _color(text, "33")


def _dim(text: str) -> str:
    return _color(text, "2")


def _add_ref_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "ref", nargs="?", help="PR number, URL, or branch (default: current branch's PR)"
    )


def _gh_ref_args(args: argparse.Namespace) -> list[str]:
    return [args.ref] if args.ref else []


# ---------------------------------------------------------------------------
# thin passthroughs — gh already does these in one call; zh just gives them a
# uniform `pr` verb and lets `zh` be the only CLI surface an agent needs

def cmd_create(args: argparse.Namespace) -> int:
    cmd = ["gh", "pr", "create"]
    if args.title:
        cmd += ["--title", args.title]
    if args.body:
        cmd += ["--body", args.body]
    if args.body_file:
        cmd += ["--body-file", args.body_file]
    if args.base:
        cmd += ["--base", args.base]
    if args.head:
        cmd += ["--head", args.head]
    if args.draft:
        cmd.append("--draft")
    if args.fill:
        cmd.append("--fill")
    for label in args.label or []:
        cmd += ["--label", label]
    for reviewer in args.reviewer or []:
        cmd += ["--reviewer", reviewer]
    for assignee in args.assignee or []:
        cmd += ["--assignee", assignee]
    if args.milestone:
        cmd += ["--milestone", args.milestone]
    if args.web:
        cmd.append("--web")
    return gh.run_passthrough(cmd)


def cmd_close(args: argparse.Namespace) -> int:
    cmd = ["gh", "pr", "close", *_gh_ref_args(args)]
    if args.comment:
        cmd += ["--comment", args.comment]
    if args.delete_branch:
        cmd.append("--delete-branch")
    return gh.run_passthrough(cmd)


def cmd_comment(args: argparse.Namespace) -> int:
    cmd = ["gh", "pr", "comment", *_gh_ref_args(args)]
    if args.body:
        cmd += ["--body", args.body]
    if args.body_file:
        cmd += ["--body-file", args.body_file]
    if args.edit_last:
        cmd.append("--edit-last")
    if args.delete_last:
        cmd.append("--delete-last")
    if args.create_if_none:
        cmd.append("--create-if-none")
    return gh.run_passthrough(cmd)


def cmd_review(args: argparse.Namespace) -> int:
    cmd = ["gh", "pr", "review", *_gh_ref_args(args)]
    if args.approve:
        cmd.append("--approve")
    elif args.request_changes:
        cmd.append("--request-changes")
    else:
        cmd.append("--comment")
    if args.body:
        cmd += ["--body", args.body]
    if args.body_file:
        cmd += ["--body-file", args.body_file]
    return gh.run_passthrough(cmd)


def cmd_label(args: argparse.Namespace) -> int:
    if not args.add and not args.remove:
        print("error: pass --add and/or --remove", file=sys.stderr)
        return 1
    cmd = ["gh", "pr", "edit", *_gh_ref_args(args)]
    for name in args.add or []:
        cmd += ["--add-label", name]
    for name in args.remove or []:
        cmd += ["--remove-label", name]
    return gh.run_passthrough(cmd)


def cmd_reviewer(args: argparse.Namespace) -> int:
    if not args.add and not args.remove:
        print("error: pass --add and/or --remove", file=sys.stderr)
        return 1
    cmd = ["gh", "pr", "edit", *_gh_ref_args(args)]
    for login in args.add or []:
        cmd += ["--add-reviewer", login]
    for login in args.remove or []:
        cmd += ["--remove-reviewer", login]
    return gh.run_passthrough(cmd)


# ---------------------------------------------------------------------------
# review threads — gh has no subcommand for these at all

def _print_thread(t: gh.ReviewThread) -> None:
    status = _dim("(resolved)") if t.is_resolved else _red("(unresolved)")
    loc = f"{t.path}:{t.line}" if t.path else ""
    print(f"{t.id}  {loc}  {status}")
    for c in t.comments:
        print(f"    {c.author}  {_dim(c.created_at)}")
        for line in c.body.splitlines():
            print(f"      {line}")


def cmd_threads(args: argparse.Namespace) -> int:
    try:
        repo = gh.repo_info()
        pr = gh.resolve_pr(args.ref)
        threads = gh.review_threads(repo.owner, repo.name, pr.number)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    shown = threads if args.all else [t for t in threads if not t.is_resolved]
    if args.path:
        shown = [t for t in shown if t.path and args.path in t.path]

    if not shown:
        print("no matching review threads")
        return 0
    for t in shown:
        _print_thread(t)
        print()
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    try:
        url = gh.reply_to_thread(args.thread_id, args.body)
        if args.resolve:
            gh.resolve_thread(args.thread_id)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(_green(f"replied: {url}"))
    if args.resolve:
        print(_green("resolved"))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        ids = list(args.thread_id)
        if args.all:
            repo = gh.repo_info()
            pr = gh.resolve_pr(args.ref)
            threads = gh.review_threads(repo.owner, repo.name, pr.number)
            for t in threads:
                if t.is_resolved:
                    continue
                if args.path and not (t.path and args.path in t.path):
                    continue
                ids.append(t.id)
        if not ids:
            print("no threads to resolve", file=sys.stderr)
            return 1
        for thread_id in ids:
            ok = gh.resolve_thread(thread_id)
            print(f"{_green('resolved') if ok else _red('failed')}  {thread_id}")
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_unresolve(args: argparse.Namespace) -> int:
    try:
        for thread_id in args.thread_id:
            ok = gh.unresolve_thread(thread_id)
            print(f"{_green('unresolved') if ok else _red('failed')}  {thread_id}")
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# merge / ship — the real bundling: preflight checks (draft, review decision,
# unresolved threads, CI) folded into the merge call instead of an agent
# chaining `gh pr view` + `gh pr checks` + review-thread queries + `gh pr
# merge` by hand

_MERGE_POLL_INTERVAL_SECONDS = 15


def _wait_for_checks(ref: str | None, timeout: int) -> gh.PullRequest:
    """Poll the PR until no check is still pending, or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    pr = gh.resolve_pr(ref)
    while any(gh.is_pending(c) for c in pr.checks) and time.monotonic() < deadline:
        pending = [c.name for c in pr.checks if gh.is_pending(c)]
        print(_dim(f"       waiting on CI: {', '.join(pending)}"))
        time.sleep(_MERGE_POLL_INTERVAL_SECONDS)
        pr = gh.resolve_pr(ref)
    return pr


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        pr = gh.resolve_pr(args.ref)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"pr     #{pr.number} {pr.title}")
    print(f"       {pr.url}")

    if not args.force:
        if pr.is_draft:
            print("error: PR is a draft — mark ready or pass --force", file=sys.stderr)
            return 1
        if pr.review_decision == "CHANGES_REQUESTED":
            print("error: changes requested — address review or pass --force", file=sys.stderr)
            return 1

        try:
            repo = gh.repo_info()
            threads = gh.review_threads(repo.owner, repo.name, pr.number)
        except gh.ZithubError:
            threads = []
        unresolved = [t for t in threads if not t.is_resolved]
        if unresolved:
            print(
                f"error: {len(unresolved)} unresolved review thread(s) — "
                "resolve them, or pass --force",
                file=sys.stderr,
            )
            return 1

        if args.wait:
            pr = _wait_for_checks(args.ref, args.timeout)
        failed = [c for c in pr.checks if gh.is_failed(c)]
        pending = [c for c in pr.checks if gh.is_pending(c)]
        if failed:
            names = ", ".join(c.name for c in failed)
            print(f"error: failing check(s): {names}", file=sys.stderr)
            return 1
        if pending:
            names = ", ".join(c.name for c in pending)
            print(f"error: still pending: {names} (rerun with --wait, or pass --force)", file=sys.stderr)
            return 1

    cmd = ["gh", "pr", "merge", *_gh_ref_args(args), f"--{args.method}"]
    if args.delete_branch:
        cmd.append("--delete-branch")
    if args.admin:
        cmd.append("--admin")
    if args.auto:
        cmd.append("--auto")
    if args.subject:
        cmd += ["--subject", args.subject]
    if args.body:
        cmd += ["--body", args.body]
    return gh.run_passthrough(cmd)


def _add_merge_args(p: argparse.ArgumentParser, default_delete_branch: bool) -> None:
    _add_ref_arg(p)
    p.add_argument(
        "--method",
        choices=["squash", "merge", "rebase"],
        default="squash",
        help="merge strategy (default: squash)",
    )
    p.add_argument(
        "--delete-branch",
        dest="delete_branch",
        action="store_true",
        default=default_delete_branch,
        help="delete the branch after merging",
    )
    p.add_argument(
        "--keep-branch",
        dest="delete_branch",
        action="store_false",
        help="keep the branch after merging",
    )
    p.add_argument("--admin", action="store_true", help="bypass branch protection as an admin")
    p.add_argument("--auto", action="store_true", help="enable auto-merge instead of merging now")
    p.add_argument("--subject", "-t", help="merge commit subject")
    p.add_argument("--body", "-b", help="merge commit body")
    p.add_argument(
        "--force",
        action="store_true",
        help="skip the draft/review/unresolved-thread/CI preflight and merge immediately",
    )
    p.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        default=True,
        help="don't poll pending checks to completion — fail immediately if any are still running",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="max seconds to wait on pending checks (default: 1200)",
    )
    p.set_defaults(func=cmd_merge)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zh", description="batched gh PR actions for AI agents"
    )
    sub = parser.add_subparsers(dest="command")
    pr = sub.add_parser("pr", help="PR write actions")
    pr_sub = pr.add_subparsers(dest="pr_command", required=True)

    p_create = pr_sub.add_parser("create", help="open a pull request")
    p_create.add_argument("-t", "--title")
    p_create.add_argument("-b", "--body")
    p_create.add_argument("-F", "--body-file", dest="body_file")
    p_create.add_argument("-B", "--base")
    p_create.add_argument("-H", "--head")
    p_create.add_argument("-d", "--draft", action="store_true")
    p_create.add_argument("-f", "--fill", action="store_true", help="use commit info for title/body")
    p_create.add_argument("-l", "--label", action="append")
    p_create.add_argument("-r", "--reviewer", action="append")
    p_create.add_argument("-a", "--assignee", action="append")
    p_create.add_argument("-m", "--milestone")
    p_create.add_argument("-w", "--web", action="store_true")
    p_create.set_defaults(func=cmd_create)

    p_close = pr_sub.add_parser("close", help="close a pull request")
    _add_ref_arg(p_close)
    p_close.add_argument("-c", "--comment", help="leave a closing comment")
    p_close.add_argument("-d", "--delete-branch", action="store_true")
    p_close.set_defaults(func=cmd_close)

    p_comment = pr_sub.add_parser("comment", help="add a general PR comment")
    _add_ref_arg(p_comment)
    p_comment.add_argument("-b", "--body")
    p_comment.add_argument("-F", "--body-file", dest="body_file")
    p_comment.add_argument("--edit-last", action="store_true")
    p_comment.add_argument("--delete-last", action="store_true")
    p_comment.add_argument("--create-if-none", action="store_true")
    p_comment.set_defaults(func=cmd_comment)

    p_review = pr_sub.add_parser("review", help="approve / request changes / comment")
    _add_ref_arg(p_review)
    review_group = p_review.add_mutually_exclusive_group()
    review_group.add_argument("--approve", action="store_true")
    review_group.add_argument("--request-changes", action="store_true")
    review_group.add_argument("--comment", dest="review_comment_flag", action="store_true")
    p_review.add_argument("-b", "--body")
    p_review.add_argument("-F", "--body-file", dest="body_file")
    p_review.set_defaults(func=cmd_review)

    p_label = pr_sub.add_parser("label", help="add/remove labels")
    _add_ref_arg(p_label)
    p_label.add_argument("--add", action="append", metavar="NAME")
    p_label.add_argument("--remove", action="append", metavar="NAME")
    p_label.set_defaults(func=cmd_label)

    p_reviewer = pr_sub.add_parser("reviewer", help="add/remove reviewers")
    _add_ref_arg(p_reviewer)
    p_reviewer.add_argument("--add", action="append", metavar="LOGIN")
    p_reviewer.add_argument("--remove", action="append", metavar="LOGIN")
    p_reviewer.set_defaults(func=cmd_reviewer)

    p_threads = pr_sub.add_parser(
        "threads", help="list review-comment threads (with the ids reply/resolve need)"
    )
    _add_ref_arg(p_threads)
    p_threads.add_argument("--all", action="store_true", help="also show resolved threads")
    p_threads.add_argument("--path", help="only threads on a path containing this substring")
    p_threads.set_defaults(func=cmd_threads)

    p_reply = pr_sub.add_parser("reply", help="reply to a review-comment thread")
    p_reply.add_argument("thread_id", help="thread id, from `zh pr threads`")
    p_reply.add_argument("-b", "--body", required=True)
    p_reply.add_argument("--resolve", action="store_true", help="also resolve the thread")
    p_reply.set_defaults(func=cmd_reply)

    p_resolve = pr_sub.add_parser("resolve", help="mark review-comment thread(s) resolved")
    p_resolve.add_argument("thread_id", nargs="*", help="thread id(s), from `zh pr threads`")
    p_resolve.add_argument("--all", action="store_true", help="resolve every unresolved thread")
    p_resolve.add_argument("--path", help="with --all, only threads on a matching path")
    p_resolve.add_argument(
        "--pr", dest="ref", help="PR number, URL, or branch to use with --all (default: current branch's PR)"
    )
    p_resolve.set_defaults(func=cmd_resolve)

    p_unresolve = pr_sub.add_parser("unresolve", help="reopen review-comment thread(s)")
    p_unresolve.add_argument("thread_id", nargs="+", help="thread id(s), from `zh pr threads`")
    p_unresolve.set_defaults(func=cmd_unresolve)

    p_merge = pr_sub.add_parser(
        "merge",
        help="merge a PR — preflights draft/review/unresolved-threads/CI first",
    )
    _add_merge_args(p_merge, default_delete_branch=False)

    p_ship = pr_sub.add_parser(
        "ship",
        help="merge + delete branch: `merge` with cleanup defaults on",
    )
    _add_merge_args(p_ship, default_delete_branch=True)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(1)
    sys.exit(args.func(args))
