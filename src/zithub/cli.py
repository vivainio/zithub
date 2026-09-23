"""zh: what's up with this repo, plus the `gh` PR/release actions for AI
agents — status/CI/PR/my/review/issues reporting (so zh works standalone,
without wazup installed alongside it), and batched write-side actions like
PR merge preflight, review-thread reply/resolve, and release
preflight/create, each as one call instead of several chained `gh`/API
calls. Actions `gh` already does in a single call (create, close, comment,
review, label, reviewer) are left to `gh` itself."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from . import board, gh, plan, registry, skills, ticket, versioning


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


def _bold(text: str) -> str:
    return _color(text, "1")


def _add_ref_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "ref", nargs="?", help="PR number, URL, or branch (default: current branch's PR)"
    )


def _gh_ref_args(args: argparse.Namespace) -> list[str]:
    return [args.ref] if args.ref else []


def _add_why_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-w",
        "--why",
        action="store_true",
        help="for failed checks, show the tail of the failing job's log",
    )


# ---------------------------------------------------------------------------
# status/ci — read-side reporting ported from wazup, so zh doesn't require it
# installed alongside just to answer "what's up with this repo/PR/CI run?"

def _check_icon(conclusion: str | None, status: str) -> str:
    s = (status or "").upper()
    c = (conclusion or "").upper()
    if s in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"}:
        return _yellow("~")
    if c == "SUCCESS" or s == "SUCCESS":
        return _green("✓")
    if c in {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"} or s in {
        "FAILURE",
        "ERROR",
    }:
        return _red("✗")
    return _dim("?")


def _fetch_failure_info(
    checks: list[gh.CheckRun], why: bool
) -> list[tuple[str | None, str | None]]:
    """(summary, log tail) per check, fetched concurrently — each is an
    independent `gh` call, so threads (I/O-bound, stdlib-only) turn N
    sequential round trips into ~1."""
    if not checks:
        return []

    def fetch(c: gh.CheckRun) -> tuple[str | None, str | None]:
        summary = gh.failed_steps_summary(c.details_url)
        tail = gh.failure_log_tail(c.details_url) if why else None
        return summary, tail

    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        return list(pool.map(fetch, checks))


def _print_failure_detail(c: gh.CheckRun, tail: str | None) -> None:
    if tail:
        print(_dim(f"       ── {c.name} (last lines before failure) ──"))
        for line in tail.splitlines():
            print(f"       {_dim('|')} {line}")
    elif c.details_url:
        print(f"       see: {c.details_url}")


def _print_why_hint(cmd: str) -> None:
    # spells out the exact command (not just "pass --why") since this is
    # parsed by AI agents as often as read by humans, and a vague hint gets
    # ignored in favor of the agent improvising raw `gh`/`git` commands
    print(f"       {_dim(f'run `{cmd} --why` to see the failing log')}")


def _print_checks(checks: list[gh.CheckRun], cmd: str, why: bool = False) -> bool:
    """Prints the check list; returns whether the *other*, unlisted repo
    workflow runs shown alongside it are also clean (see
    _print_recent_other_runs) — combine with _all_checks_passed(checks) for
    the overall verdict."""
    if not checks:
        print("ci     no checks reported")
    else:
        print("ci")
        failed = [c for c in checks if gh.is_failed(c)]
        info = iter(_fetch_failure_info(failed, why))

        for c in checks:
            print(f"       {_check_icon(c.conclusion, c.status)} {c.name}")
            if gh.is_failed(c):
                summary, tail = next(info)
                if summary:
                    print(f"         {_dim(summary)}")
                if why:
                    _print_failure_detail(c, tail)
        if failed and not why:
            _print_why_hint(cmd)
    return _print_recent_other_runs(
        exclude_names={c.name for c in checks},
        exclude_urls={c.details_url for c in checks},
    )


def _staleness_note(head_sha: str | None) -> str | None:
    """None if a run's commit is HEAD (or its distance from HEAD can't be
    determined locally); otherwise a note that it predates HEAD by N
    commits — e.g. a path-filtered workflow (docs, release) whose last run
    is several commits behind because nothing since has touched its paths,
    so a failure shown here may already be moot."""
    if not head_sha:
        return None
    behind = gh.commits_ahead_of(head_sha)
    if not behind:
        return None
    return _dim(
        f"stale — ran on {head_sha[:7]}, {behind} commit{'s' if behind != 1 else ''} "
        "behind HEAD; may already be fixed"
    )


def _print_recent_other_runs(exclude_names: set[str], exclude_urls: set[str | None]) -> bool:
    """Other workflows' runs elsewhere in the repo — still active, or
    completed within the last hour — that weren't already shown above.
    E.g. a release-triggered publish workflow, invisible to the
    branch-scoped CI lookup since it isn't scoped to this branch. Only the
    most recent run per workflow name is shown. Returns False if any shown
    run has failed, so callers can fold it into their "clean" verdict
    instead of it only ever being a footnote."""
    try:
        runs = gh.recent_other_runs()
    except gh.ZithubError:
        return True
    seen: set[str] = set()
    ok = True
    for r in runs:
        if r.name in exclude_names or r.url in exclude_urls or r.name in seen:
            continue
        seen.add(r.name)
        icon = _check_icon(r.conclusion, r.status)
        success = (r.conclusion or "").upper() == "SUCCESS" or (r.status or "").upper() == "SUCCESS"
        if success:
            # a clean pass needs no link — same reasoning as _print_ci_fallback
            age = gh.run_age_minutes(r.created_at)
            suffix = f"  {_dim(f'{age}m ago')}" if age is not None else ""
            print(f"       {icon} {r.name}{suffix}")
        else:
            print(f"       {icon} {r.name}  {r.url}")
            if gh.is_failed(gh.CheckRun(r.name, r.status, r.conclusion, r.url)):
                ok = False
                note = _staleness_note(r.head_sha)
                if note:
                    print(f"         {note}")
    return ok


def _print_ci_fallback(branch: str, why: bool, cmd: str) -> tuple[bool, bool]:
    """CI status for a branch with no open PR, from its latest workflow
    runs — this is what `pr` would show if there were a PR to attach to.
    Returns (found, passed): whether any runs were found, and whether the
    latest one succeeded."""
    runs = gh.latest_runs_for_branch(branch, limit=10)
    if not runs:
        return False, False

    checks = [gh.CheckRun(r.name, r.status, r.conclusion, r.url) for r in runs]
    latest = checks[0]
    earlier_failures = sum(1 for c in checks[1:] if gh.is_failed(c))

    print("ci")
    icon = _check_icon(latest.conclusion, latest.status)
    if gh.is_success(latest):
        # a clean pass needs no link — the URL only earns its keep when
        # there's something to click through to (a failure, or a run still
        # in flight worth watching)
        print(f"       {icon} {latest.name}")
    else:
        print(f"       {icon} {latest.name}  {latest.details_url}")

    if not gh.is_failed(latest):
        if earlier_failures:
            note = f"fixed — {earlier_failures} of the last {len(checks)} runs had failed"
            print(f"         {_dim(note)}")
        other_ok = _print_recent_other_runs(
            exclude_names={latest.name}, exclude_urls={latest.details_url}
        )
        return True, gh.is_success(latest) and other_ok

    summary, tail = _fetch_failure_info([latest], why)[0]
    if summary:
        print(f"         {_dim(summary)}")
    if why:
        _print_failure_detail(latest, tail)
    stale = _staleness_note(runs[0].head_sha)
    if stale:
        print(f"         {stale}")
    if earlier_failures:
        print(f"         {_dim(f'{earlier_failures + 1} of the last {len(checks)} runs failed')}")
    if not why:
        _print_why_hint(cmd)
    _print_recent_other_runs(exclude_names={latest.name}, exclude_urls={latest.details_url})
    return True, False


def _pr_suffix(prs: dict[str, gh.BranchPr], branch: str) -> str:
    pr = prs.get(branch)
    if not pr:
        return ""
    draft = " draft" if pr.is_draft else ""
    return f"  {_green(f'PR #{pr.number}{draft}')}"


def _worktree_status_note(unmerged: int | None, dirty: bool) -> str:
    # "merged" is a safe-to-prune claim, so it must not appear next to
    # "dirty" — pruning a dirty worktree loses uncommitted work regardless
    # of whether its commits are merged.
    if dirty:
        return f"  {_red('dirty')}"
    if unmerged is None:
        return ""
    if unmerged == 0:
        return f"  {_dim('merged')}"
    return f"  {_yellow(f'{unmerged} unmerged')}"


def _print_recent_branches(current_branch: str) -> None:
    # called only when current_branch is the repo's default branch, so it
    # doubles as the ref worktrees are checked for unmerged commits against
    worktrees = gh.worktree_branches()
    all_branches = gh.local_branches_by_recency(exclude={current_branch, *worktrees})
    branches = [b for b in all_branches if not gh.is_orphaned_worktree_branch_name(b.name)][:8]
    prs = gh.open_prs_by_branch()

    if branches:
        print("recent branches")
        for b in branches:
            print(f"       {b.name}  ({b.relative_date}){_pr_suffix(prs, b.name)}")

    worktree_entries = gh.worktree_info(
        exclude_branch=current_branch, default_ref=f"origin/{current_branch}"
    )
    if worktree_entries:
        print("worktrees")
        for w in worktree_entries:
            path = _dim(_display_path(w.path))
            print(
                f"       {w.branch}  ({w.relative_date})"
                f"{_pr_suffix(prs, w.branch)}{_worktree_status_note(w.unmerged, w.dirty)}  {path}"
            )


_MAX_LISTED_CHANGED_FILES = 10


def _print_local_status(status: gh.LocalStatus) -> None:
    if status.ahead is None:
        push_note = _dim("no upstream")
    else:
        notes = []
        if status.ahead > 0:
            notes.append(f"{status.ahead} unpushed")
        if status.behind:
            ff_note = " (fast-forward)" if not status.ahead else ""
            notes.append(f"{status.behind} behind{ff_note}")
        push_note = _yellow(", ".join(notes)) if notes else _green("pushed")

    tree_note = _red("dirty") if status.changed_files else _green("clean")
    untracked_note = (
        _dim(f"  (+{status.untracked_count} untracked)") if status.untracked_count else ""
    )
    print(f"local  {push_note}, {tree_note}{untracked_note}")

    if len(status.changed_files) > _MAX_LISTED_CHANGED_FILES:
        print(f"       {len(status.changed_files)} files changed")
    else:
        for f in status.changed_files:
            print(f"       {f.status} {f.path}")


def _print_worktree_note(repo: gh.RepoInfo, branch: str, local: gh.LocalStatus) -> None:
    """For a linked worktree (not the repo's main checkout) on a non-default
    branch, show how it stands against the default branch — the "ahead of
    origin" figure in the `local` line is against this branch's own
    upstream, if any, which says nothing about whether the work here has
    already made it into main via a squash/rebase merge elsewhere."""
    if branch == repo.default_branch or not gh.is_linked_worktree():
        return
    origin_ref = f"origin/{repo.default_branch}"
    ahead_origin = gh.commits_ahead_of(origin_ref)
    ahead = ahead_origin
    ref_label = origin_ref
    if ahead is None:
        ahead = gh.commits_ahead_of(repo.default_branch)
        ref_label = f"local {repo.default_branch}"
    if ahead is None:
        return
    if ahead > 0:
        # It may still be fully merged locally (e.g. a local ff-merge that
        # hasn't been pushed) even though origin/main doesn't have it yet —
        # worth saying so instead of just "not in origin/main", since that
        # alone reads as unmerged work rather than an unpushed merge.
        if ahead_origin is not None and ahead_origin > 0:
            ahead_local = gh.commits_ahead_of(repo.default_branch)
            if ahead_local == 0:
                note = _yellow(f"merged into local {repo.default_branch}, not pushed to {origin_ref}")
                print(f"worktree  {note}")
                return
        note = _yellow(f"{ahead} commit{'s' if ahead != 1 else ''} not in {ref_label}")
        print(f"worktree  {note}")
        return

    print(f"worktree  {_dim(f'merged into {ref_label}')}")
    # merged is not enough on its own — deleting a dirty worktree loses
    # uncommitted work regardless of what's already landed on main.
    if local.changed_files or local.untracked_count:
        return
    root = gh.worktree_root()
    if root:
        print(f"          {_dim(f'safe to delete: git worktree remove {root}')}")


def _is_local_clean(status: gh.LocalStatus) -> bool:
    return not status.changed_files and not status.behind and not (status.ahead or 0)


def _all_checks_passed(checks: list[gh.CheckRun]) -> bool:
    return bool(checks) and all(gh.is_success(c) for c in checks)


def _print_all_clean_if(local_clean: bool, ci_ok: bool) -> None:
    # An explicit, imperative line rather than a bare status word — an AI
    # agent parsing this output should be able to stop analyzing right here
    # without re-deriving "clean" from the local/ci lines above.
    if local_clean and ci_ok:
        print(_green("Everything is clean — nothing to do, no need to dig further."))


def cmd_status(args: argparse.Namespace) -> int:
    fetch_thread = gh.start_background_fetch()
    try:
        repo = gh.repo_info()
        branch = gh.current_branch()
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"repo   {repo.name_with_owner}  ({repo.url})")
    print(f"branch {branch}" + (" (default)" if branch == repo.default_branch else ""))
    fetch_thread.join(timeout=5)  # cap the wait; a slow fetch just means stale ahead/behind
    local = gh.local_status()
    _print_local_status(local)
    _print_worktree_note(repo, branch, local)

    pr = gh.current_pr()
    if pr is None:
        print("pr     none")
        try:
            found, ci_ok = _print_ci_fallback(branch, args.why, cmd="zh")
        except gh.ZithubError:
            found, ci_ok = False, False
        if not found:
            print("ci     none")
        if branch == repo.default_branch:
            _print_recent_branches(current_branch=branch)
        _print_all_clean_if(local_clean=_is_local_clean(local), ci_ok=found and ci_ok)
        return 0

    state = pr.state.lower() + (" (draft)" if pr.is_draft else "")
    print(f"pr     #{pr.number} {pr.title}  [{state}]")
    print(f"       {pr.url}")
    if pr.review_decision:
        print(f"review {pr.review_decision.replace('_', ' ').lower()}")

    other_ok = _print_checks(pr.checks, cmd="zh", why=args.why)
    _print_all_clean_if(
        local_clean=_is_local_clean(local), ci_ok=_all_checks_passed(pr.checks) and other_ok
    )
    return 0


def _print_review_threads(threads: list[gh.ReviewThread], show_resolved: bool) -> None:
    shown = threads if show_resolved else [t for t in threads if not t.is_resolved]
    if not shown:
        label = "no review comments" if show_resolved else "no unresolved review comments"
        print(f"comments  {_green(label)}")
        return

    unresolved_count = sum(1 for t in shown if not t.is_resolved)
    print(f"comments  {_yellow(f'{unresolved_count} unresolved')}" + (
        f", {len(shown) - unresolved_count} resolved" if show_resolved else ""
    ))
    for t in shown:
        status = _dim("(resolved)") if t.is_resolved else _red("(unresolved)")
        for c in t.comments:
            loc = f"{c.path}:{c.line}" if c.path else ""
            print(f"       {c.author}  {loc}  {status}")
            for line in c.body.splitlines():
                print(f"         {_dim(line)}")
        print()
    if not show_resolved and len(shown) < len(threads):
        print(f"       {_dim('run `zh pr --all` to also see resolved threads')}")


def cmd_pr_status(args: argparse.Namespace) -> int:
    try:
        repo = gh.repo_info()
        branch = gh.current_branch()
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"repo   {repo.name_with_owner}  ({repo.url})")

    pr = gh.current_pr()
    if pr is None:
        print(f"branch {branch}  (no open PR)")
        try:
            mine = gh.my_open_prs_in_repo()
        except gh.ZithubError:
            mine = []
        if mine:
            print(f"your open PRs in {repo.name_with_owner}:")
            _print_pr_list(mine, show_repo=False)
        return 1

    state = pr.state.lower() + (" (draft)" if pr.is_draft else "")
    print(f"pr     #{pr.number} {pr.title}  [{state}]")
    print(f"       {pr.url}")
    if pr.review_decision:
        print(f"review {pr.review_decision.replace('_', ' ').lower()}")

    ci_ok = _print_checks(pr.checks, cmd="zh pr", why=args.why) and _all_checks_passed(pr.checks)

    threads = gh.review_threads(repo.owner, repo.name, pr.number)
    _print_review_threads(threads, show_resolved=args.all)

    unresolved = any(not t.is_resolved for t in threads)
    if ci_ok and not unresolved:
        print(_green("Everything is clean — nothing to do, no need to dig further."))
    return 0


def cmd_ci(args: argparse.Namespace) -> int:
    try:
        branch = gh.current_branch()
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pr = gh.current_pr()
    if pr is not None:
        print(f"pr #{pr.number} {pr.title}")
        _print_checks(pr.checks, cmd="zh ci", why=args.why)
        return 0

    print(f"branch {branch}  (no open PR)")
    try:
        found, _ci_ok = _print_ci_fallback(branch, args.why, cmd="zh ci")
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not found:
        print(f"no CI runs found for branch {branch}")
    return 0


def _parse_iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _print_pr_line(p: gh.PullRequestSummary, indent: str, show_state: bool = False) -> None:
    draft = _dim(" draft") if p.is_draft else ""
    age = _dim(_relative_age(_parse_iso(p.updated_at)))
    state = _dim(f"  [{p.state.lower()}]") if show_state else ""
    print(f"{indent}#{p.number} {p.title}{draft}{state}  {age}")
    print(f"{indent}    {_dim(p.url)}")


def _group_by_repo(prs: list[gh.PullRequestSummary]) -> dict[str, list[gh.PullRequestSummary]]:
    by_repo: dict[str, list[gh.PullRequestSummary]] = {}
    for p in prs:
        by_repo.setdefault(p.repo or "?", []).append(p)
    return by_repo


def _print_pr_list(prs: list[gh.PullRequestSummary], show_repo: bool, show_closed: bool = False) -> None:
    if not prs:
        print("  none")
        return
    if not show_repo:
        for p in prs:
            _print_pr_line(p, indent="  ")
        return

    # Cross-repo: surface open PRs grouped by repo; collapse merged/closed
    # noise (routine over a wide window) to per-repo counts, unless
    # show_closed asks for the full breakdown instead.
    open_prs = [p for p in prs if p.state.upper() == "OPEN"]
    other_prs = [p for p in prs if p.state.upper() != "OPEN"]

    open_by_repo = _group_by_repo(open_prs)
    print(_green(f"open ({len(open_prs)})"))
    for repo in sorted(open_by_repo):
        print(f"  {repo}")
        for p in open_by_repo[repo]:
            _print_pr_line(p, indent="    ")

    if not other_prs:
        return

    if show_closed:
        print(_dim(f"closed/merged ({len(other_prs)})"))
        by_repo = _group_by_repo(other_prs)
        for repo in sorted(by_repo):
            print(f"  {repo}")
            for p in by_repo[repo]:
                _print_pr_line(p, indent="    ", show_state=True)
    else:
        counts: dict[str, int] = {}
        for p in other_prs:
            counts[p.repo or "?"] = counts.get(p.repo or "?", 0) + 1
        summary = ", ".join(f"{repo} ({n})" for repo, n in sorted(counts.items()))
        print(_dim(f"closed/merged in window ({len(other_prs)}): {summary}  (--closed for details)"))


def _print_issue_list(issues: list[gh.IssueSummary], show_repo: bool) -> None:
    if not issues:
        print("  none")
        return
    for i in issues:
        prefix = f"{i.repo}  " if show_repo and i.repo else ""
        print(f"  {prefix}#{i.number} {i.title}  [{i.state.lower()}]  {i.updated_at}")
        print(f"      {i.url}")


def cmd_my(args: argparse.Namespace) -> int:
    try:
        if args.this:
            repo = gh.repo_info()
            print(f"your open PRs in {repo.name_with_owner}:")
            _print_pr_list(gh.my_open_prs_in_repo(), show_repo=False)
        else:
            since = (date.today() - timedelta(days=args.days)).isoformat()
            print(f"your open PRs across all repos, plus closed/merged since {since}:")
            _print_pr_list(gh.my_recent_prs(since), show_repo=True, show_closed=args.closed)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# CI can finish (or be re-run) without bumping the PR's updatedAt, so a
# cached row in one of these states is refetched even when unchanged.
_BOARD_UNSETTLED_CI_STATES = {"pending", "failed"}


def cmd_board_sync(args: argparse.Namespace) -> int:
    """Incremental: list your open PRs cheaply, drop cached rows that are no
    longer open, then fetch full detail only for PRs that are new, updated
    since they were cached, or had unsettled CI — in small batches, each
    saved as it lands, so a re-run after a failure resumes rather than
    starting over."""
    host = gh.current_host()
    synced_at = datetime.now(timezone.utc).isoformat()
    try:
        refs = gh.board_pr_list(host)
        login = gh.current_login(host)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if login:
        board.set_login(host, login)
    board.prune(host, {(r.repo, r.number) for r in refs})

    cached = board.cached_versions(host)
    todo = [
        r
        for r in refs
        if args.full
        or (r.repo, r.number) not in cached
        or cached[(r.repo, r.number)][0] != r.updated_at
        or cached[(r.repo, r.number)][1] in _BOARD_UNSETTLED_CI_STATES
    ]
    fetched = 0
    for i in range(0, len(todo), gh.BOARD_DETAILS_BATCH_SIZE):
        batch = todo[i : i + gh.BOARD_DETAILS_BATCH_SIZE]
        try:
            prs = gh.board_pr_details(host, [r.id for r in batch])
        except gh.ZithubError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(
                f"saved {fetched}/{len(todo)} PR(s) needing refresh — re-run `zh board sync` to continue",
                file=sys.stderr,
            )
            return 1
        board.upsert(host, prs, synced_at)
        fetched += len(batch)
        if len(todo) > gh.BOARD_DETAILS_BATCH_SIZE:
            print(f"  fetched {fetched}/{len(todo)}", file=sys.stderr)
    print(
        f"synced {len(refs)} open PR(s) from {host} ({len(todo)} refreshed, "
        f"{len(refs) - len(todo)} unchanged) to {board.db_path(host)}"
    )
    return 0


def _days_idle(iso_timestamp: str) -> int:
    then = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - then).days


def _print_board_row(r: board.BoardRow) -> None:
    idle = _days_idle(r.last_activity_at)
    idle_label = f"idle {idle}d"
    idle_label = _yellow(idle_label) if idle >= 7 else idle_label
    draft = " (draft)" if r.is_draft else ""
    review = r.review_decision.lower().replace("_", " ") or "no review"
    last_comment = f"  last comment: {r.last_comment_author}" if r.last_comment_author else ""
    print(f"{r.repo}#{r.number}{draft}  {r.title}")
    print(
        f"    {idle_label}  ci: {r.ci_state}  review: {review}  "
        f"comments: {r.comment_count}{last_comment}"
    )
    print(f"    {r.url}")


def cmd_board(args: argparse.Namespace) -> int:
    host = gh.current_host()
    rows = board.list_board(host)
    if not rows:
        print(f"no synced PRs for {host} — run `zh board sync` first  ({board.db_path(host)})")
        return 0
    stale_days = args.stale_days
    shown = 0
    for r in rows:
        idle = _days_idle(r.last_activity_at)
        if stale_days is not None and idle < stale_days:
            continue
        shown += 1
        _print_board_row(r)
    if stale_days is not None and shown == 0:
        print(f"no PRs idle >= {stale_days}d")
    return 0


def cmd_board_focus(args: argparse.Namespace) -> int:
    host = gh.current_host()
    rows = board.list_board(host)
    if not rows:
        print(f"no synced PRs for {host} — run `zh board sync` first  ({board.db_path(host)})")
        return 0
    login = board.get_login(host)

    fix_ci = [r for r in rows if r.ci_state == "failed"]
    needs_reply = [
        r
        for r in rows
        if r not in fix_ci and r.last_comment_author and r.last_comment_author != login
    ]
    ready_to_merge = [
        r
        for r in rows
        if r not in fix_ci
        and r not in needs_reply
        and not r.is_draft
        and r.review_decision == "APPROVED"
        and r.ci_state in ("success", "none")
    ]
    accounted_for = {id(r) for r in fix_ci + needs_reply + ready_to_merge}
    waiting = [r for r in rows if id(r) not in accounted_for]

    def _section(title: str, section_rows: list[board.BoardRow]) -> None:
        if not section_rows:
            return
        print(_bold(f"{title} ({len(section_rows)})"))
        for r in section_rows:
            _print_board_row(r)
        print()

    _section("fix CI", fix_ci)
    _section("needs your reply", needs_reply)
    _section("ready to merge", ready_to_merge)
    if waiting:
        print(_dim(f"waiting on others: {len(waiting)} PR(s), no action needed right now"))
    if not (fix_ci or needs_reply or ready_to_merge or waiting):
        print("nothing to focus on")
    return 0


def cmd_board_query(args: argparse.Namespace) -> int:
    try:
        columns, rows = board.run_query(gh.current_host(), args.sql)
    except board.BoardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # sqlite3 errors (bad SQL, unknown column, etc.)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no rows")
        return 0
    widths = [
        max(len(str(col)), *(len(str(row[i])) for row in rows)) for i, col in enumerate(columns)
    ]
    print("  ".join(col.ljust(w) for col, w in zip(columns, widths)))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    since = (date.today() - timedelta(days=7)).isoformat()
    print(f"PRs awaiting your review, updated since {since}:")
    try:
        _print_pr_list(gh.prs_awaiting_my_review(since), show_repo=True)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    try:
        repo = gh.current_repo()
        if repo is not None:
            print(f"your open issues in {repo.name_with_owner}:")
            _print_issue_list(gh.my_open_issues_in_repo(), show_repo=False)
        else:
            since = (date.today() - timedelta(days=7)).isoformat()
            print(f"your open issues with activity since {since}:")
            _print_issue_list(gh.my_open_issues(since), show_repo=True)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


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
# check — sanity checks beyond CI/review: what branch this targets, and
# whether it references a ticket, both otherwise only visible by opening
# the PR on the web

def _local_checkout_for(repo_name: str | None, branch: str) -> registry.RepoEntry | None:
    if not repo_name:
        return None
    return next(
        (e for e in registry.list_seen() if e.repo == repo_name and e.branch == branch), None
    )


def _check_one(ref: str | None) -> bool:
    """Prints one PR's summary block: target branch, its local checkout (if
    any, from the `zh repos` registry) and whether that checkout is dirty,
    and whether it references a ticket. Returns whether it passed (a ticket
    reference was found) — a dirty local checkout only warns."""
    try:
        pr = gh.resolve_pr(ref)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False

    print(f"pr     #{pr.number} {pr.title}")
    print(f"       {pr.url}")
    print(f"target {pr.base_ref_name}  <- {pr.head_ref_name}")

    checkout = _local_checkout_for(gh.repo_for_ref(ref), pr.head_ref_name)
    if checkout is None:
        print(_dim(f"local  no known checkout of {pr.head_ref_name} — see `zh repos`"))
    else:
        print(f"local  {_dim(_display_path(checkout.path))}")
        dirty = gh.dirty_files(checkout.path)
        if dirty:
            print(
                _yellow(
                    f"       dirty — {len(dirty)} uncommitted change(s); "
                    "be careful switching branches or pulling there"
                )
            )

    result = ticket.check_ticket_reference(f"{pr.title}\n{pr.body}")
    if not result.found:
        print(
            _red(
                "ticket no reference found in title/body — add a GitHub issue "
                "ref (#123), a tracker URL, or a ticket key"
            )
        )
        return False

    print(f"ticket {_green(f'{result.kind}: {result.detail}')}")
    if result.note:
        print(f"       {_yellow(result.note)}")
    return True


def cmd_check(args: argparse.Namespace) -> int:
    refs: list[str | None] = args.ref or [None]
    all_ok = True
    for i, ref in enumerate(refs):
        if i > 0:
            print()
        all_ok = _check_one(ref) and all_ok
    return 0 if all_ok else 1


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
# plan — draft every PR write action into one reviewable file, then apply it
# in one go (see plan.py for the format)

_PR_REF_RE = re.compile(r"^(\d+|https?://\S+/pull/\d+\S*)$")


def _plan_generate(args: argparse.Namespace) -> int:
    if args.ref and not _PR_REF_RE.match(args.ref):
        print(f"error: expected a PR number or URL, got {args.ref!r} (branch names aren't supported)", file=sys.stderr)
        return 1
    try:
        repo = gh.repo_info()
        pr = gh.resolve_pr(args.ref)
        threads = gh.review_threads(repo.owner, repo.name, pr.number)
    except gh.ZithubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = plan.render(repo.name_with_owner, pr, threads, show_resolved=args.all)
    if not args.output:
        sys.stdout.write(text)
        return 0
    if os.path.exists(args.output) and not args.force:
        print(f"error: {args.output} exists (it may hold edits) — pass --force to overwrite", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text)
    print(_green(f"wrote {args.output}"), file=sys.stderr)
    return 0


def _plan_problems(
    p: plan.Plan, repo: gh.RepoInfo, pr: gh.PullRequest, threads: list[gh.ReviewThread]
) -> list[str]:
    """Everything that would make applying `p` fail or act on something the
    plan's author never saw — checked before any action runs."""
    problems: list[str] = []
    if p.repo != repo.name_with_owner:
        problems.append(f"plan is for {p.repo}, but this directory is {repo.name_with_owner}")
    if pr.head_sha != p.head:
        problems.append(
            f"stale plan: PR head moved {p.head[:7]} -> {pr.head_sha[:7]} — regenerate with `zh plan`"
        )
    if pr.state != "OPEN":
        problems.append(f"PR #{pr.number} is {pr.state.lower()}, not open")

    resolved = {t.id: t.is_resolved for t in threads}
    for a in p.actions:
        if a.kind in ("reply", "resolve", "unresolve"):
            for tid in a.args:
                if tid not in resolved:
                    problems.append(f"line {a.line}: no such thread on #{pr.number}: {tid}")
                elif a.kind == "resolve":
                    resolved[tid] = True
                elif a.kind == "unresolve":
                    resolved[tid] = False
        else:  # merge / ship: judged against thread state after the earlier actions
            if pr.is_draft:
                problems.append(f"line {a.line}: PR is a draft")
            if pr.review_decision == "CHANGES_REQUESTED":
                problems.append(f"line {a.line}: changes requested")
            open_threads = sum(1 for r in resolved.values() if not r)
            if open_threads:
                problems.append(f"line {a.line}: {open_threads} thread(s) still unresolved after this plan")
            failed = [c.name for c in pr.checks if gh.is_failed(c)]
            if failed:
                problems.append(f"line {a.line}: failing check(s): {', '.join(failed)}")
    return problems


def _plan_describe(a: plan.Action) -> str:
    if a.kind == "reply":
        return f"reply to {a.args[0]} ({len(a.body)} chars)"
    if a.kind in ("resolve", "unresolve"):
        return f"{a.kind} {', '.join(a.args)}"
    return f"{a.kind} ({a.args[0]})"


def _plan_run(a: plan.Action, number: int) -> None:
    if a.kind == "reply":
        print(_green(f"replied: {gh.reply_to_thread(a.args[0], a.body)}"))
    elif a.kind == "resolve":
        for tid in a.args:
            if not gh.resolve_thread(tid):
                raise gh.ZithubError(f"could not resolve {tid}")
            print(_green(f"resolved  {tid}"))
    elif a.kind == "unresolve":
        for tid in a.args:
            gh.unresolve_thread(tid)
            print(_green(f"unresolved  {tid}"))
    else:
        rc = cmd_merge(
            argparse.Namespace(
                ref=str(number),
                method=a.args[0],
                delete_branch=a.kind == "ship",
                admin=False,
                auto=False,
                subject=None,
                body=None,
                force=False,
                wait=True,
                timeout=1200,
            )
        )
        if rc != 0:
            raise gh.ZithubError(f"{a.kind} failed")


def _plan_apply(args: argparse.Namespace) -> int:
    try:
        if args.apply == "-":
            text = sys.stdin.read()
        else:
            with open(args.apply, encoding="utf-8") as f:
                text = f.read()
        p = plan.parse(text)
        repo = gh.repo_info()
        pr = gh.resolve_pr(str(p.number))
        threads = gh.review_threads(repo.owner, repo.name, pr.number)
    except (OSError, plan.PlanError, gh.ZithubError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    problems = _plan_problems(p, repo, pr, threads)
    if problems:
        for msg in problems:
            print(f"error: {msg}", file=sys.stderr)
        print("nothing was applied", file=sys.stderr)
        return 1
    if not p.actions:
        print("plan has no actions — nothing to do")
        return 0

    print(f"plan   {p.repo}#{p.number}  {pr.title}")
    for i, a in enumerate(p.actions, start=1):
        print(f"  {i}. {_plan_describe(a)}")
    if args.dry_run:
        print(_dim("dry run — nothing was applied"))
        return 0

    for i, a in enumerate(p.actions):
        try:
            _plan_run(a, p.number)
        except gh.ZithubError as exc:
            print(f"error: step {i + 1} ({_plan_describe(a)}): {exc}", file=sys.stderr)
            print(f"applied {i} of {len(p.actions)}; not run:", file=sys.stderr)
            for rest in p.actions[i:]:
                print(f"  - {_plan_describe(rest)}", file=sys.stderr)
            return 1
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    if args.apply:
        return _plan_apply(args)
    if args.dry_run:
        print("error: --dry-run only applies with --apply", file=sys.stderr)
        return 1
    return _plan_generate(args)


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
        relation = gh.ahead_behind(remote_sha)
        if relation is None:
            fix = f"push with `git push origin {target}` or reconcile"
        else:
            ahead, behind = relation
            if behind == 0:
                fix = f"push with `git push origin {target}`"
            elif ahead == 0:
                fix = f"pull with `git pull origin {target}`"
            else:
                fix = "reconcile the diverged history"
        print(
            f"error: local HEAD ({sha[:12]}) != origin/{target} ({remote_sha[:12]}) — "
            f"{fix}, then retry",
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
    """The literal command to run once notes are written. Points at
    `zh release create` without `--preflight` — the preflight was just run
    right here, and skipping it (the default) means creating doesn't redo
    it, while still getting the publish-workflow wait `zh` adds over plain
    `gh release create`."""
    target_flag = f" --target {target_arg}" if target_arg else ""
    return f'zh release create {tag} --notes "..." --title {tag}{target_flag}'


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
    if args.preflight:
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
        "--preflight",
        action="store_true",
        help="run the pre-release checks (CI, sync, dirty) before creating",
    )


# ---------------------------------------------------------------------------
# repos — local-checkout registry, ported from wazup: recorded as a side
# effect of ordinary zh usage, queried so an agent can find a checkout by
# name instead of guessing paths or re-cloning

def _display_path(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def _relative_age(ts: float) -> str:
    seconds = max(0.0, time.time() - ts)
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    months = days / 30
    if months < 12:
        return f"{int(months)}mo ago"
    return f"{int(months / 12)}y ago"


def cmd_repos(args: argparse.Namespace) -> int:
    entries = registry.find_seen(args.query) if args.query else registry.list_seen()
    if not entries:
        msg = (
            f"no known checkouts matching '{args.query}'"
            if args.query
            else "no known checkouts yet — run zh inside a repo to register it"
        )
        print(msg, file=sys.stderr)
        return 1
    for e in entries:
        print(f"{e.repo}  {e.branch}  ({_relative_age(e.last_seen)})  {_dim(_display_path(e.path))}")
    return 0


def _record_repo_seen() -> None:
    """Best-effort upsert of the current checkout into the `zh repos`
    registry — local git plumbing only, no `gh` call. No-ops outside a repo
    or on any failure, same as wazup's version of this."""
    try:
        name = gh.remote_name_with_owner()
        path = gh.worktree_root()
        if not name or not path:
            return
        branch = gh.current_branch()
    except gh.ZithubError:
        return
    registry.record_seen(path, name, branch)


def _owner_token_env() -> dict[str, str] | None:
    """os.environ plus GH_TOKEN for the gh account that owns origin, or None
    (inherit as-is) when there's no such account or the caller already set
    GH_TOKEN/GITHUB_TOKEN themselves."""
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return None
    found = gh.repo_owner_token()
    if found is None:
        return None
    account, token = found
    if not account.active:
        print(_dim(f"using gh account {account.login} (owner of this repo)"), file=sys.stderr)
    return {**os.environ, "GH_TOKEN": token}


def cmd_gh(gh_args: list[str]) -> int:
    return gh.run_passthrough(["gh", *gh_args], env=_owner_token_env())


# Makes git authenticate through gh's credential helper — which honors
# GH_TOKEN — even if the user's git config names some other helper.
_GH_CREDENTIAL_HELPER = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


def cmd_git(git_args: list[str]) -> int:
    """git over an https origin as the repo owner's gh account (push, fetch,
    pull of a private repo, ...). With an ssh origin the ssh key decides
    the account, so it's plain git."""
    env = _owner_token_env() if gh.origin_is_https() else None
    if env is None:
        return gh.run_passthrough(["git", *git_args])
    return gh.run_passthrough(["git", *_GH_CREDENTIAL_HELPER, *git_args], env=env)


# Commands whose arguments go verbatim to another CLI. argparse can't take
# a REMAINDER that starts with an option (`zh gh --version`, `zh git -C x`),
# so main() dispatches these before parsing; their subparsers exist only so
# they show up in `zh --help`.
_PASSTHROUGH_COMMANDS = {"gh": cmd_gh, "git": cmd_git}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zh", description="what's up with this repo, plus the gh PR/release actions"
    )
    parser.set_defaults(needs_gh=True)
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="repo, branch, local, PR, and CI status in one shot")
    p_status.set_defaults(func=cmd_status, needs_gh=True)
    _add_why_flag(p_status)

    p_ci = sub.add_parser("ci", help="show CI status for the current branch/PR")
    p_ci.set_defaults(func=cmd_ci)
    _add_why_flag(p_ci)

    pr = sub.add_parser("pr", help="PR status, or actions gh doesn't already do in one call")
    pr.add_argument(
        "--all", action="store_true", help="also show resolved review comment threads"
    )
    _add_why_flag(pr)
    pr.set_defaults(func=cmd_pr_status)
    pr_sub = pr.add_subparsers(dest="pr_command", required=False)

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

    p_check = pr_sub.add_parser(
        "check",
        help="target branch, local checkout, and ticket reference — for one PR or several",
    )
    p_check.add_argument(
        "ref",
        nargs="*",
        help="PR number(s), URL(s), or branch(es) (default: current branch's PR)",
    )
    p_check.set_defaults(func=cmd_check)

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

    p_plan = sub.add_parser(
        "plan",
        help="draft PR actions into one reviewable file (`zh plan > plan.md`), then `--apply` it",
    )
    p_plan.add_argument(
        "ref", nargs="?", help="PR number or URL (default: current branch's PR)"
    )
    p_plan.add_argument("--all", action="store_true", help="also list resolved threads in the scaffold")
    p_plan.add_argument("-o", "--output", help="write the scaffold to this file instead of stdout")
    p_plan.add_argument("--force", action="store_true", help="with -o, overwrite an existing file")
    p_plan.add_argument(
        "--apply", metavar="FILE", help="validate and run a plan file ('-' for stdin) instead of generating one"
    )
    p_plan.add_argument("--dry-run", action="store_true", help="with --apply, show the steps without running them")
    p_plan.set_defaults(func=cmd_plan)

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
        "create", help="create the release at an explicit version (add --preflight to check first)"
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

    p_my = sub.add_parser("my", help="list your open PRs across all repos, plus recent closed/merged")
    p_my.add_argument(
        "--days", type=int, default=30, help="lookback window in days for closed/merged PRs (default: 30)"
    )
    p_my.add_argument(
        "--closed", action="store_true", help="show closed/merged PRs in full, not just counts"
    )
    p_my.add_argument(
        "--this", action="store_true", help="scope to the current repo instead of all repos"
    )
    p_my.set_defaults(func=cmd_my)

    p_board = sub.add_parser(
        "board", help="local sqlite view of your open PRs, synced from GitHub (`zh board sync` first)"
    )
    p_board.add_argument(
        "--stale-days", type=int, default=None, metavar="N", help="only show PRs idle for at least N days"
    )
    p_board.set_defaults(func=cmd_board, needs_gh=False)
    board_sub = p_board.add_subparsers(dest="board_command")

    p_board_sync = board_sub.add_parser("sync", help="fetch your open PRs and their activity into the local db")
    p_board_sync.add_argument(
        "--full", action="store_true", help="refetch every PR, not just new/changed ones"
    )
    p_board_sync.set_defaults(func=cmd_board_sync)

    p_board_query = board_sub.add_parser("query", help="run a read-only SQL query against the synced db")
    p_board_query.add_argument("sql", help='e.g. "select repo, number, title from prs where ci_state=\'failed\'"')
    p_board_query.set_defaults(func=cmd_board_query, needs_gh=False)

    p_board_focus = board_sub.add_parser(
        "focus", help="grouped view of the synced PRs: fix CI, needs your reply, ready to merge, waiting on others"
    )
    p_board_focus.set_defaults(func=cmd_board_focus, needs_gh=False)

    p_review = sub.add_parser("review", help="list PRs awaiting your review, updated this week")
    p_review.set_defaults(func=cmd_review)

    p_issues = sub.add_parser(
        "issues", help="list your open issues in this repo, or recent issue activity outside a repo"
    )
    p_issues.set_defaults(func=cmd_issues)

    p_repos = sub.add_parser(
        "repos",
        help="list local checkouts zh has seen, so agents can find one without guessing paths",
    )
    p_repos.add_argument(
        "query", nargs="?", help="only show checkouts whose repo name or path contains this"
    )
    p_repos.set_defaults(func=cmd_repos, needs_gh=False)

    p_install_skills = sub.add_parser(
        "install-skills",
        help="install the zh Claude Code skill to $CLAUDE_CONFIG_DIR/skills/ (default ~/.claude/skills/)",
    )
    p_install_skills.add_argument(
        "--skills-dir",
        metavar="DIR",
        help="target directory for skills (default: $CLAUDE_CONFIG_DIR/skills, or ~/.claude/skills)",
    )
    p_install_skills.set_defaults(func=skills.install_skills_command, needs_gh=False)

    p_gh = sub.add_parser(
        "gh", add_help=False, help="run gh with GH_TOKEN set to this repo owner's account (no global switch)"
    )
    p_gh.add_argument("args", nargs=argparse.REMAINDER)
    p_gh.set_defaults(func=lambda a: cmd_gh(a.args), needs_gh=False)

    p_git = sub.add_parser(
        "git", add_help=False, help="run git; over https, as this repo owner's gh account (no global switch)"
    )
    p_git.add_argument("args", nargs=argparse.REMAINDER)
    p_git.set_defaults(func=lambda a: cmd_git(a.args), needs_gh=False)

    return parser


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in _PASSTHROUGH_COMMANDS:
        sys.exit(_PASSTHROUGH_COMMANDS[argv[0]](argv[1:]))
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        # bare `zh` is deliberately inert: no gh calls, no registry writes
        parser.print_help()
        sys.exit(0)
    if args.command != "install-skills":
        skills.check_skill_staleness()
    if args.needs_gh:
        _record_repo_seen()
        notice = gh.ensure_gh_account_for_repo()
        if notice:
            print(_dim(notice), file=sys.stderr)
    sys.exit(args.func(args))
