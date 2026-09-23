# zithub

`zh` — what's up with this repo, right now, plus batched `gh` PR and release
actions for AI agents. A standalone `gh`/`git` CLI: bare `zh` gives repo,
branch, local, PR, and CI status in one shot (no separate status tool
required), and the rest covers what plain `gh` can't already do in a single
call — acting on PR review-comment threads (no `gh` subcommand exists for
these at all), merging with a real preflight
(draft/review-decision/unresolved-threads/CI, checked and merged in one
call instead of chained separately), and release preflight/creation. The
status/CI/PR/my/review/issues reporting is ported from
[wazup](https://github.com/vivainio/wazup), so `zh` doesn't need wazup
installed alongside it. It deliberately does *not* wrap `gh pr
create/close/comment/review/edit`, since those are already one `gh` call
each and an agent that already knows `gh` gains nothing from a second name
for the same thing.

Requires the [GitHub CLI](https://cli.github.com/) (`gh`) installed and
authenticated (`gh auth login`).

## Commands

```
zh                # prints help (bare zh does nothing)
zh status         # repo, branch, local, PR, and CI status in one shot
                  # (also recent branches/worktrees on the default branch)
zh ci             # just the CI status for the current branch/PR
zh pr             # current branch's PR: status, checks, and review comment
                  # threads (unresolved by default; --all also shows resolved)
zh my             # your open PRs across all repos, plus recent closed/merged
zh my --this      # ...just for the current repo
zh review         # PRs awaiting your review, updated in the last 7 days
zh issues         # your open issues in this repo, or recent activity outside one
zh gh <args...>   # gh <args...> as this repo owner's gh account (GH_TOKEN; no global switch)
zh git <args...>  # git <args...>; over https, as this repo owner's gh account (same)

zh board sync             # fetch your open PRs + activity (CI, review, comments) into a local sqlite db
                          # (incremental: only new/updated PRs or unsettled CI; --full refetches all)
zh board                  # list synced PRs, oldest activity first (--stale-days N to filter)
zh board focus            # grouped: fix CI, needs your reply, ready to merge, waiting on others
zh board query "<SQL>"    # ad hoc read-only SQL against the synced db
                          # (one board per GitHub site: $GH_HOST, else origin's host, else github.com)

zh pr threads    # list review-comment threads (with the ids below)
zh pr reply      # reply to a review-comment thread, optionally --resolve
zh pr resolve    # mark thread(s) resolved, by id or --all
zh pr unresolve  # reopen thread(s)

zh plan          # draft reply/resolve/merge actions into ONE reviewable file
                 # (context as comments, all actions commented out): zh plan > plan.md
zh plan --apply plan.md [--dry-run]   # validate everything, then run it in order

zh pr check      # target branch, and whether the PR references a ticket

zh pr merge      # merge a PR
zh pr ship       # merge + delete branch (same as `merge --delete-branch`)

zh release                    # preflight, then commits + the gh command for each next version
zh release patch/minor/major  # preflight, then just that one bumped version + gh command
zh release create             # preflight, then actually create at an explicit version

zh repos           # local checkouts zh has seen, most-recently-seen first
zh install-skills  # install the zh Claude Code skill
```

Add `-w`/`--why` to `zh`, `zh ci`, or `zh pr` to drill into a failing check
— prints the tail of the failing job's log, right up to the error, instead
of just a pass/fail icon.

`threads`/`reply`/`resolve`/`unresolve` exist because `gh` has no
subcommand for review threads at all — they go through `gh api graphql`
directly (`reviewThreads`, `resolveReviewThread`, `unresolveReviewThread`,
`addPullRequestReviewThreadReply`). Run `zh pr threads` to see each
thread's id, then act on it:

```
zh pr threads                       # unresolved threads on the current branch's PR
zh pr reply T_kwDOA... -b "fixed in a1b2c3d" --resolve
zh pr resolve --all --path src/     # resolve every unresolved thread under src/
```

`zh pr check` surfaces what you'd otherwise have to open the PR on the web
(or find a local clone) to see: the base branch it's targeting (no
judgment — just shown, since only you know whether it's the wrong one),
its local checkout if `zh repos` has seen one for that branch — warning if
that checkout is dirty, since switching branches or pulling there would be
risky — and whether the title/body references a ticket at all: a GitHub
issue ref (`#123`, `Fixes #123`), any URL (covers a Jira/Linear/etc. link),
or a bare Jira-style key (`ABC-123`, flagged with a suggestion to link it
directly). Exits non-zero if no ticket reference is found.

Typically called with one or more PR URLs directly (an owner/repo parsed
right out of the URL, so it works from anywhere — not just a checkout of
that repo), rather than a bare number, which only resolves against the
current directory's repo:

```
zh pr check                                          # current branch's PR
zh pr check https://github.com/acme/widgets/pull/42
zh pr check https://github.com/.../pull/42 https://github.com/.../pull/43   # a summary block per PR
```

`zh pr merge` (and `zh pr ship`, which is the same thing with
`--delete-branch` on by default) bundles what would otherwise be a
draft/review-decision/unresolved-thread/CI check spread across several
calls into one preflight, then merges only if everything's clean:

```
zh pr merge                 # preflight, waiting on any pending checks, then squash-merge
zh pr ship                  # same, plus delete the branch afterward
zh pr merge --no-wait        # fail fast instead of polling pending checks
zh pr merge --force          # skip the preflight and merge immediately
zh pr merge --method rebase --keep-branch
```

`zh release` (bare, or `patch`/`minor`/`major`) checks: the active gh
account matches the repo's owner (same convention `wazup` uses — a
logged-in account whose login, or the org half of a `name_OrgName`
corporate-SSO login, matches origin's owner; best-effort and never blocks
the release if nothing matches), the current branch matches the release
target, local HEAD matches `origin/<target>` exactly, and CI is green on
that exact commit (falling back to the branch's latest runs if no run
exists yet for the commit, and proceeding on judgement if none exist at
all). A dirty worktree only warns — it isn't part of the release either
way, since the tag points at HEAD.

Past the preflight it's read-only: it never creates anything itself. It
shows the commit log a release's notes get written from, and prints the
exact `zh release create ...` command to run once notes are ready. That
command skips its own preflight by default, so running it right after
`zh release` doesn't redo the CI wait that was just run here.
`patch`/`minor`/`major` fetch the latest release tag and compute one
specific bump; bare `zh release` shows all three:

```
zh release                          # -> zh release create <next patch/minor/major> ...
zh release patch                    # -> just the patch bump
zh release patch --target release-2.0
```

`zh release create` is the one command that actually publishes — it
requires release notes (`-n`/`--notes` or `-F`/`--notes-file` — one of
them, always), and after creating the GitHub release it also polls for and
waits on any workflow run(s) triggered by that release (e.g. a PyPI
publish job), reporting success or failure instead of leaving that for the
caller to check separately. A repo with no such workflow just proceeds —
it's not treated as a failure. The preflight is opt-in via `--preflight`,
for when you're calling it directly without a prior `zh release`:

```
zh release create v1.3.0 -n "$(cat notes.md)"
zh release create v2.0.0-rc1 --prerelease --preflight   # check first, since there was no prior `zh release`
```

Every command above that needs `gh` also registers the checkout it ran in
— path, repo, branch — into a local registry at
`~/.local/share/zithub/repos.jsonl`, ported from wazup's own (a separate
database, same design). `zh repos` lists what's been seen, letting an
agent find a local checkout of a repo by name instead of guessing paths or
re-cloning; two worktrees or clones of the same repo, at different paths,
show up as separate entries with their own branch:

```
zh repos          # everything seen, most-recently-seen first
zh repos zithub   # only checkouts whose repo name or path contains "zithub"
```

## Install

```
uv tool install zithub
```

## License

MIT
