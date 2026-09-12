"""zh: batched `gh` PR/release actions for AI agents — PR merge preflight,
review-thread reply/resolve, and release preflight/create, each as one call
instead of several chained `gh`/API calls. Actions `gh` already does in a
single call (create, close, comment, review, label, reviewer) are left to
`gh` itself."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from . import gh, versioning


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


# ---------------------------------------------------------------------------
# release preflight / create — ports the github-release skill's preflight.py
# (gh auth, target branch, remote sync, CI) so `release create` can gate on
# it directly: one call that checks readiness and creates the release,
# instead of a separate preflight script plus a hand-typed `gh release
# create`.

_CI_QUICK_ATTEMPTS = 5
_CI_QUICK_INTERVAL_SECONDS = 1
_CI_POLL_INTERVAL_SECONDS = 20
_CI_MAX_ATTEMPTS = 30


def _print_run_failures(runs: list[gh.WorkflowRun]) -> None:
    for r in runs:
        print(f"       {_red(r.name)}={r.conclusion}  {r.url}")
        tail = gh.run_failure_log(r.database_id)
        if tail:
            print(_dim(f"       ── {r.name} (log tail) ──"))
            for line in tail.splitlines():
                print(f"       {_dim('|')} {line}")


def _wait_for_release_ci(sha: str, branch: str) -> bool:
    """True if CI passed (or no CI exists at all — proceed on judgement),
    False if it failed or never completed. Mirrors the github-release
    skill's preflight polling: a short grace period for the run to appear,
    then a longer poll while anything is still in progress."""
    runs: list[gh.WorkflowRun] = []
    for attempt in range(_CI_MAX_ATTEMPTS):
        runs = gh.runs_for_commit(sha)
        if not runs:
            if attempt >= _CI_QUICK_ATTEMPTS:
                break
            time.sleep(_CI_QUICK_INTERVAL_SECONDS)
            continue
        pending = [r for r in runs if r.status != "completed"]
        if not pending:
            break
        names = ", ".join(f"{r.name}={r.status}" for r in runs)
        print(_dim(f"       waiting on CI: {names}"))
        time.sleep(_CI_POLL_INTERVAL_SECONDS)

    if not runs:
        latest = gh.latest_runs_for_branch(branch, limit=5)
        failed = [r for r in latest if r.status == "completed" and r.conclusion != "success"]
        if failed:
            print(f"ci     {_red('no run for this commit; latest ' + branch + ' runs failed')}")
            _print_run_failures(failed)
            return False
        print(_dim("ci     no run found for this commit — proceed with judgement"))
        return True

    pending = [r for r in runs if r.status != "completed"]
    if pending:
        names = ", ".join(f"{r.name}={r.status}" for r in pending)
        print(f"error: CI did not complete on {sha[:12]}: {names}", file=sys.stderr)
        return False

    failed = [r for r in runs if r.conclusion != "success"]
    if failed:
        print(f"ci     {_red('failed')}", file=sys.stderr)
        _print_run_failures(failed)
        return False

    names = ", ".join(r.name for r in runs)
    print(f"ci     {_green('success')} ({names})")
    return True


def _release_preflight(target_arg: str | None) -> tuple[str, str, list[gh.ReleaseInfo]] | None:
    """Runs every preflight check, printing progress as it goes. Returns
    (target_branch, head_sha, recent_releases) on pass, None on failure (the
    specific check that failed has already printed its own error)."""
    try:
        branch = gh.current_branch()
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    notice = gh.ensure_gh_account_for_repo()
    if notice:
        print(_dim(notice))
    active = next((a for a in gh.list_gh_accounts() if a.active), None)
    print(f"auth   {active.login if active else '?'}")

    try:
        repo = gh.repo_info()
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    target = target_arg or repo.default_branch
    if branch != target:
        override = "" if target_arg else f" or rerun with `--target {branch}`"
        print(
            f"error: current branch is '{branch}', but release target is "
            f"'{target}'; check out '{target}'{override}",
            file=sys.stderr,
        )
        return None
    print(f"branch {branch}  (release target)")

    try:
        releases = gh.recent_releases()
    except gh.ZithubError:
        releases = []
    if releases:
        print("releases")
        for r in releases:
            kind = " (draft)" if r.is_draft else " (prerelease)" if r.is_prerelease else ""
            print(f"       {r.tag_name}{kind}  {r.published_at}")

    try:
        dirty = gh.dirty_files()
    except gh.ZithubError:
        dirty = []
    if dirty:
        print(_yellow("local  dirty — do not release until everything is committed:"))
        for line in dirty:
            print(f"       {line}")
    else:
        print(f"local  {_green('clean')}")

    try:
        gh.fetch_all()
        sha = gh.current_commit_sha()
        remote_sha = gh.remote_branch_sha(target)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if remote_sha is None:
        print(
            f"error: origin/{target} not found — push with `git push -u origin {target}` first",
            file=sys.stderr,
        )
        return None
    if sha != remote_sha:
        print(
            f"error: local HEAD ({sha[:12]}) != origin/{target} ({remote_sha[:12]}) — "
            f"push with `git push origin {target}` or reconcile, then retry",
            file=sys.stderr,
        )
        return None
    print(f"sync   ok ({sha[:12]})")

    if not _wait_for_release_ci(sha, target):
        return None

    return target, sha, releases


def _print_commits(previous_tag: str | None) -> None:
    try:
        commits = gh.commits_since(previous_tag)
    except gh.ZithubError:
        commits = []
    if not commits:
        return
    label = f"commits since {previous_tag}" if previous_tag else "commits (first release)"
    print(f"\n{label}:")
    for c in commits:
        print(f"       {c}")


def _gh_release_create_hint(tag: str, target_arg: str | None) -> str:
    """The literal `gh release create` command to run once notes are
    written. Points at plain `gh`, not `zh release create` — the preflight
    that command would redo (including the CI wait) was just run right
    here, so re-running it a moment later just to create is wasted work."""
    target_flag = f" --target {target_arg}" if target_arg else ""
    return f'gh release create {tag} --notes "..." --title {tag}{target_flag}'


def cmd_release(args: argparse.Namespace) -> int:
    """Bare `zh release`: full preflight, then — instead of just pass/fail —
    the commit log a release's notes get written from, plus the exact next
    command (with the version already computed) for each bump size."""
    result = _release_preflight(args.target)
    if result is None:
        return 1
    target, _sha, releases = result
    print(_green("PREFLIGHT PASS"))

    previous_tag = releases[0].tag_name if releases else None
    _print_commits(previous_tag)
    print()

    if previous_tag is None:
        print("next   no previous release — write notes, pick a starting version, then:")
        print(f"       {_gh_release_create_hint('<version>', args.target)}")
        return 0

    print("next   write release notes from the commits above, then:")
    for part in ("patch", "minor", "major"):
        try:
            candidate = versioning.bump(previous_tag, part)
        except versioning.VersionError:
            print(
                f"       can't auto-bump '{previous_tag}' (not a plain vX.Y.Z tag) — "
                "pick the next version yourself"
            )
            break
        print(f"       {_gh_release_create_hint(candidate, args.target)}   ({part})")
    return 0


_PUBLISH_QUICK_ATTEMPTS = 5
_PUBLISH_QUICK_INTERVAL_SECONDS = 2
_PUBLISH_POLL_INTERVAL_SECONDS = 10
_PUBLISH_MAX_ATTEMPTS = 60


def _wait_for_release_workflows(since: str) -> bool:
    """Poll for workflow run(s) triggered by this release's `release:
    published` event (e.g. a PyPI publish job) that were created at or
    after `since` (an ISO-8601 UTC cutoff captured just before the release
    was created — string comparison sorts correctly since gh's createdAt is
    fixed-width ISO-8601). True if every matching run succeeds, or none
    ever appears (plenty of repos have no such workflow, and this isn't
    reason to call the release itself a failure) — False if any run fails
    or is still going once the timeout is hit."""
    seen: dict[int, gh.WorkflowRun] = {}
    for attempt in range(_PUBLISH_MAX_ATTEMPTS):
        for r in gh.runs_for_event("release"):
            if (r.created_at or "") >= since:
                seen[r.database_id] = r

        if not seen:
            if attempt >= _PUBLISH_QUICK_ATTEMPTS:
                print(_dim("       no workflow triggered by the release — nothing to wait on"))
                return True
            time.sleep(_PUBLISH_QUICK_INTERVAL_SECONDS)
            continue

        pending = [r for r in seen.values() if r.status != "completed"]
        if not pending:
            break
        names = ", ".join(f"{r.name}={r.status}" for r in pending)
        print(_dim(f"       waiting on release workflow(s): {names}"))
        time.sleep(_PUBLISH_POLL_INTERVAL_SECONDS)
    else:
        names = ", ".join(f"{r.name}={r.status}" for r in seen.values() if r.status != "completed")
        print(f"error: release workflow(s) did not complete in time: {names}", file=sys.stderr)
        return False

    failed = [r for r in seen.values() if r.conclusion != "success"]
    if failed:
        print("error: release workflow(s) failed:", file=sys.stderr)
        _print_run_failures(failed)
        return False

    print(f"publish {_green('success')} ({', '.join(r.name for r in seen.values())})")
    return True


def _finish_release(tag: str, target: str | None, args: argparse.Namespace) -> int:
    notes = args.notes
    if args.notes_file:
        with open(args.notes_file, encoding="utf-8") as f:
            notes = f.read()

    since = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        url = gh.create_release(
            tag=tag,
            notes=notes,
            title=args.title or tag,
            target=target,
            draft=args.draft,
            prerelease=args.prerelease,
        )
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(_green(f"released: {url}"))

    if not _wait_for_release_workflows(since):
        return 1
    return 0


def cmd_release_create(args: argparse.Namespace) -> int:
    target = args.target
    if not args.force:
        result = _release_preflight(args.target)
        if result is None:
            return 1
        target, _sha, _releases = result
    return _finish_release(args.tag, target, args)


def cmd_release_bump(args: argparse.Namespace) -> int:
    """`zh release patch/minor/major`: preflight + compute the next version
    from the latest release tag, then print it and the exact `release
    create` command to run. Never creates the release itself — bumping the
    version and choosing to release it are kept as two explicit steps, so
    nothing gets published just because a version number was requested."""
    target = args.target
    releases: list[gh.ReleaseInfo] = []
    if not args.force:
        result = _release_preflight(args.target)
        if result is None:
            return 1
        target, _sha, releases = result
    else:
        try:
            releases = gh.recent_releases(limit=1)
        except gh.ZithubError:
            releases = []

    if not releases:
        print(
            "error: no previous release to bump from — "
            'use `zh release create <version> --notes "..."` for the first release',
            file=sys.stderr,
        )
        return 1

    previous_tag = releases[0].tag_name
    try:
        tag = versioning.bump(previous_tag, args.part)
    except versioning.VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"version {previous_tag} -> {tag}")
    _print_commits(previous_tag)
    print()
    print(f"next   {_gh_release_create_hint(tag, args.target)}")
    return 0


def _add_release_create_args(p: argparse.ArgumentParser) -> None:
    notes_group = p.add_mutually_exclusive_group(required=True)
    notes_group.add_argument("-n", "--notes", help="release notes")
    notes_group.add_argument(
        "-F", "--notes-file", dest="notes_file", help="read release notes from file"
    )
    p.add_argument("-t", "--title", help="release title (default: the tag)")
    p.add_argument(
        "--target", metavar="BRANCH", help="release branch (default: repo's default branch)"
    )
    p.add_argument("-d", "--draft", action="store_true")
    p.add_argument("-p", "--prerelease", action="store_true")
    p.add_argument(
        "--force", action="store_true", help="skip the preflight and create immediately"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zh", description="batched gh PR actions for AI agents"
    )
    sub = parser.add_subparsers(dest="command")
    pr = sub.add_parser("pr", help="PR actions gh doesn't already do in one call")
    pr_sub = pr.add_subparsers(dest="pr_command", required=True)

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

    release = sub.add_parser(
        "release",
        help="preflight + what to release next (bare, or patch/minor/major); create to cut one",
    )
    release.add_argument(
        "--target", metavar="BRANCH", help="release branch (default: repo's default branch)"
    )
    release.set_defaults(func=cmd_release)
    release_sub = release.add_subparsers(dest="release_command")

    p_release_create = release_sub.add_parser(
        "create", help="preflight, then create the release at an explicit version"
    )
    p_release_create.add_argument("tag", help="release tag/version, e.g. v1.3.0")
    _add_release_create_args(p_release_create)
    p_release_create.set_defaults(func=cmd_release_create)

    for part in ("patch", "minor", "major"):
        p_bump = release_sub.add_parser(
            part,
            help=f"preflight, then show the {part}-bumped version and the gh command to create it",
        )
        p_bump.add_argument(
            "--target", metavar="BRANCH", help="release branch (default: repo's default branch)"
        )
        p_bump.add_argument(
            "--force", action="store_true", help="skip the preflight"
        )
        p_bump.set_defaults(func=cmd_release_bump, part=part)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(1)
    sys.exit(args.func(args))
