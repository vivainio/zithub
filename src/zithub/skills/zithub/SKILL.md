---
name: zithub
description: Check repo/branch/PR/CI status, view or act on a PR's review comment threads, merge/ship a PR, run a release preflight or cut a release, list your PRs/issues, or find a local checkout of a repo, using the zh CLI. Use when working in a git repo and you need repo/branch/PR/CI status in one shot instead of piecing it together from several `gh`/`git` calls, or when you need to merge a PR, reply to/resolve review comments, or cut a release without chaining multiple `gh` commands by hand.
updated: 2026-09-13
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
zh                    # repo, branch, PR, and CI status for the current directory
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
`zh release` exit 1 if their preflight fails; the plain status commands
(`zh`, `zh ci`, `zh my`, `zh review`, `zh issues`) exit 0 regardless of what
they report (they're informational).

## Install

```bash
uv tool install zithub
```
