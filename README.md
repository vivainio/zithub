# zithub

`zh` — batched `gh` PR actions for AI agents, for the two things plain `gh`
can't already do in a single call: acting on PR review-comment threads (no
`gh` subcommand exists for these at all), and merging with a real preflight
(draft/review-decision/unresolved-threads/CI, checked and merged in one
call instead of chained separately). Where
[wazup](https://github.com/vivainio/wazup) is the read-only "what's up with
this repo" status tool, `zh` is this narrower write-side complement — it
deliberately does *not* wrap `gh pr create/close/comment/review/edit`,
since those are already one `gh` call each and an agent that already knows
`gh` gains nothing from a second name for the same thing.

Requires the [GitHub CLI](https://cli.github.com/) (`gh`) installed and
authenticated (`gh auth login`).

## Commands

```
zh pr threads    # list review-comment threads (with the ids below)
zh pr reply      # reply to a review-comment thread, optionally --resolve
zh pr resolve    # mark thread(s) resolved, by id or --all
zh pr unresolve  # reopen thread(s)

zh pr merge      # merge a PR
zh pr ship       # merge + delete branch (same as `merge --delete-branch`)

zh release                    # preflight, then commits + the gh command for each next version
zh release patch/minor/major  # preflight, then just that one bumped version + gh command
zh release create             # preflight, then actually create at an explicit version
```

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

`zh release` (bare, or `patch`/`minor`/`major`) ports the checks from the
`github-release` skill's `preflight.py` — the right gh account is active
(tested by actually trying to view the repo, not by guessing from a
login/owner naming convention), the current branch matches the release
target, local HEAD matches `origin/<target>` exactly, and CI is green on
that exact commit (falling back to the branch's latest runs if no run
exists yet for the commit, and proceeding on judgement if none exist at
all). A dirty worktree only warns — it isn't part of the release either
way, since the tag points at HEAD.

Past the preflight it's read-only: it never creates anything itself. It
shows the commit log a release's notes get written from, and prints the
exact `gh release create ...` command to run once notes are ready — not
`zh release create`, since the preflight (including the CI wait) was just
run right here and redoing it a moment later would be wasted work.
`patch`/`minor`/`major` fetch the latest release tag and compute one
specific bump; bare `zh release` shows all three:

```
zh release                          # -> gh release create <next patch/minor/major> ...
zh release patch                    # -> just the patch bump
zh release patch --target release-2.0
```

`zh release create` is the one command that actually publishes — it runs
the same preflight itself first (unless `--force`), requires release notes
(`-n`/`--notes` or `-F`/`--notes-file` — one of them, always), and after
creating the GitHub release it also polls for and waits on any workflow
run(s) triggered by that release (e.g. a PyPI publish job), reporting
success or failure instead of leaving that for the caller to check
separately. A repo with no such workflow just proceeds — it's not treated
as a failure. So it's also safe to call directly without a prior
`zh release`:

```
zh release create v1.3.0 -n "$(cat notes.md)"
zh release create v2.0.0-rc1 --prerelease --force   # skip the preflight
```

## Install

```
uv tool install zithub
```

## License

MIT
