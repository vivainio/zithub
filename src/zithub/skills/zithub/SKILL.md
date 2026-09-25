---
name: zithub
description: Check repo/branch/PR/CI status, view or act on a PR's review comment threads, merge/ship a PR, run a release preflight or cut a release, list your PRs/issues, sync/query a local sqlite board of your open PRs, or find a local checkout of a repo, using the zh CLI. Use when working in a git repo and you need repo/branch/PR/CI status in one shot instead of piecing it together from several `gh`/`git` calls, or when you need to merge a PR, reply to/resolve review comments, cut a release, or see which of your open PRs across repos need attention right now.
updated: 2026-09-22
---

# zh

What's up with this repo, right now — plus the write-side `gh` actions `gh`
itself doesn't bundle into one call. A thin CLI over `gh` and `git` — run it
instead of making several separate `gh`/`git` calls and piecing the results
together. Requires the `gh` CLI, installed and authenticated.

**Start here:** plain `zh` (no subcommand) is usually the first command to
run in a repo — repo, branch, PR, and CI status in one shot, plus a final
"Everything is clean" line when there's nothing to act on.

## Commands

```bash
zh status             # repo, branch, PR, and CI status for the current directory (bare `zh` only prints help)
                       # (also lists recent local branches/worktrees when on the
                       # default branch with no PR of its own)
zh ci                  # just the CI status for the current branch/PR
zh pr                  # current branch's PR: status, checks, and review comment
                        # threads (unresolved by default; --all also shows resolved) —
                        # use this to see what a reviewer (or Copilot's automated
                        # review) actually said, not just approve/request-changes
zh my                  # your open PRs in this repo, or (outside a repo) all your
                        # open PRs across all repos, plus closed/merged ones from
                        # the last 30 days as per-repo counts (--days N to change
                        # that window, --closed to list them in full)
zh review              # PRs awaiting your review, updated in the last 7 days
zh issues              # your open issues in this repo, or (outside a repo) your
                        # open issues with activity in the last 7 days, across all repos

zh board sync          # fetch every open PR you authored (any repo) — review decision,
                        # aggregate CI state, comment count/last commenter — into a local
                        # sqlite db. Incremental: only PRs that are new, updated, or had
                        # pending/failed CI are refetched, in batches saved as they land —
                        # after an error, just re-run it. --full refetches everything
zh board               # synced PRs grouped per repo (biggest first), oldest-activity
                        # first within each (--stale-days N to filter)
zh board focus         # the same PRs grouped by what to do: fix CI, needs your reply
                        # (a human other than you commented last), ready to merge (approved
                        # + green + not draft), and a count of the rest that need nothing
                        # from you; rows grouped per repo within each section. Bot comments
                        # (GitHub Apps, `[bot]`, scanners like snyk-io) are ignored; org
                        # machine users go in ZH_BOT_LOGINS=a,b (applied at sync time, so
                        # re-run `zh board sync --full` after changing it)
zh board query "<SQL>" # ad hoc read-only SELECT/WITH against the synced `prs` table

zh plan [pr] > plan.md       # read-only: scaffold with every thread's context (diff hunk,
                              # comments) as # comments and all actions commented out
zh plan --apply plan.md       # after the plan is edited: validates all of it first (stale
                              # head, unknown threads, merge blockers), then runs it in order;
                              # --dry-run shows the steps. Prefer this to separate reply/
                              # resolve/merge calls: draft the plan, let the user review it,
                              # then apply once. Plan verbs: `reply <thread>` (indented body),
                              # `resolve`/`unresolve <thread>...`, `merge|ship [method]` (last)
zh pr threads [ref]           # list review-comment threads (with the ids reply/resolve need)
zh pr reply <thread_id> -b .. # reply to a review-comment thread (--resolve to also resolve it)
zh pr resolve [thread_id...]  # mark review-comment thread(s) resolved (--all for every one)
zh pr unresolve <thread_id>...# reopen review-comment thread(s)
zh pr check [ref...]          # target branch, local checkout, and ticket reference —
                               # for one PR or several
zh pr merge [ref]             # merge a PR — preflights draft/review/unresolved-threads/CI first
zh pr ship [ref]               # merge + delete branch: `merge` with cleanup defaults on

zh release             # preflight + what to release next (commit log, next version)
zh release patch/minor/major   # preflight, then show the bumped version + `gh release create` command
zh release create <tag> -n ..  # preflight, then create the release

zh repos               # local checkouts zh has seen, most-recently-seen first —
                        # use this to find where a repo already lives on disk instead
                        # of guessing a path or re-cloning it
zh repos <query>       # ...filtered to checkouts whose repo name or path contains it

zh gh <args...>        # run gh with GH_TOKEN set to the gh account that owns origin —
                        # e.g. with a work account active, `zh gh pr create` in a
                        # personal repo still acts as you; gh's active account is untouched
zh git <args...>       # git (e.g. `zh git push`); over an https origin, authenticates as
                        # that same account (gh's credential helper + GH_TOKEN). ssh: plain git.
                        # Use these instead of `gh auth switch` on a 403 from the wrong account
zh install-skills      # install this skill to $CLAUDE_CONFIG_DIR/skills/ (default ~/.claude/skills/)
```

Add `-w`/`--why` to `zh`, `zh ci`, or `zh pr` to drill into a failing check —
prints the tail of the failing job's log, right up to the error, instead of
just a pass/fail icon.

Every ordinary `zh`/`ci`/`pr`/`my`/`review`/`issues`/`release`/`repos`
invocation inside a repo also registers that checkout in the `zh repos`
registry, so running any of them is enough to make a checkout discoverable
later — no separate opt-in step.

Actions `gh` already does in a single call (create, close, comment, review,
label, reviewer) are left to `gh` itself — `zh` only wraps what takes several
chained `gh`/API calls to do by hand (merge preflight, review-thread
reply/resolve, release preflight/create).

Exit codes are meaningful: `zh repos` (with no matches) exits 1; `zh pr`
(bare) exits 1 if there's no open PR for the current branch; `zh pr merge`/
`zh release` exit 1 if their preflight fails; `zh board query` exits 1 on
anything but a `SELECT`/`WITH` statement or a SQL error; the plain status
commands (`zh status`, `zh ci`, `zh my`, `zh review`, `zh issues`, `zh
board`, `zh board focus`) exit 0 regardless of what they report (they're
informational). `zh board` is local-only and never live — run `zh board
sync` first, and again whenever the synced data might be stale. There's one
board per GitHub site (github.com, a GHES host, ...): every `zh board`
command uses the current site — `$GH_HOST` if set, else the host of the
repo's origin remote, else github.com — so sync separately from a checkout
on each site you use.

## Install

```bash
uv tool install zithub
```
