# zithub

`zh` — batched `gh` PR actions for AI agents. Where
[wazup](https://github.com/vivainio/wazup) is the read-only "what's up with
this repo" status tool, `zh` is the write side: opening, merging, closing,
labeling, reviewing, and — the part `gh` has no subcommand for at all —
replying to and resolving PR review-comment threads.

Each `zh` command packages what would otherwise be several chained
`gh`/GraphQL calls into one, so an AI agent spends fewer tool round trips
(and tokens) doing PR housekeeping.

Requires the [GitHub CLI](https://cli.github.com/) (`gh`) installed and
authenticated (`gh auth login`).

## Commands

```
zh pr create     # open a PR (title/body/base/draft/labels/reviewers/...)
zh pr close      # close a PR, optionally with a comment and branch deletion
zh pr comment    # add a general PR comment
zh pr review     # approve / request changes / comment
zh pr label      # add/remove labels
zh pr reviewer   # add/remove reviewers

zh pr threads    # list review-comment threads (with the ids below)
zh pr reply      # reply to a review-comment thread, optionally --resolve
zh pr resolve    # mark thread(s) resolved, by id or --all
zh pr unresolve  # reopen thread(s)

zh pr merge      # merge a PR
zh pr ship       # merge + delete branch (same as `merge --delete-branch`)
```

Most of the above (`create`, `close`, `comment`, `review`, `label`,
`reviewer`) are thin wrappers: `gh` already does each in one call, `zh` just
gives every PR action the same `zh pr <verb>` surface.

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

## Install

```
uv tool install zithub
```

## License

MIT
