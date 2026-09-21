"""Plan files: a reviewable, all-in-one batch of PR write actions.

`zh plan` renders a scaffold (context as `#` comments, every action
commented out); a human or AI uncomments/edits the actions it wants; `zh plan
--apply` parses, validates everything up front, then runs the actions in
order. This module is the pure part — render and parse, no I/O.

Format (one action per line; `#` at column 0 is a comment):

    @pr acme/widgets#7 head=<full sha>
    reply PRRT_abc
      indented body, de-indented by two spaces
    resolve PRRT_abc PRRT_def
    unresolve PRRT_ghi
    merge squash        # or `ship squash` (merge + delete branch); must be last
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import gh

MERGE_METHODS = ("squash", "merge", "rebase")
_HEADER_RE = re.compile(r"^@pr\s+(\S+)#(\d+)\s+head=(\S+)\s*$")
_PLACEHOLDER_BODY = "<text>"
_HUNK_CONTEXT_LINES = 8


class PlanError(Exception):
    """A plan file that can't be parsed; the message carries the line number."""


@dataclass
class Action:
    kind: str  # reply | resolve | unresolve | merge | ship
    line: int
    args: list[str] = field(default_factory=list)
    body: str = ""


@dataclass
class Plan:
    repo: str
    number: int
    head: str
    actions: list[Action]


def _quote(prefix: str, text: str) -> list[str]:
    return [f"{prefix}{line}".rstrip() for line in text.splitlines()] or [prefix.rstrip()]


def render(
    repo: str, pr: gh.PullRequest, threads: list[gh.ReviewThread], show_resolved: bool = False
) -> str:
    """The scaffold for a PR: header directive + per-thread context, with
    every action commented out so applying it untouched does nothing."""
    state = pr.state.lower() + (" (draft)" if pr.is_draft else "")
    out = [
        f"@pr {repo}#{pr.number} head={pr.head_sha}",
        "#",
        f"# {pr.title}  [{state}]",
        f"# review: {pr.review_decision.replace('_', ' ').lower() or 'none'}",
    ]
    failed = [c.name for c in pr.checks if gh.is_failed(c)]
    pending = [c.name for c in pr.checks if gh.is_pending(c)]
    if failed:
        out.append(f"# CI: failing — {', '.join(failed)}")
    elif pending:
        out.append(f"# CI: pending — {', '.join(pending)}")
    else:
        out.append(f"# CI: passing ({len(pr.checks)} checks)")
    out += [
        "#",
        "# Everything below is commented out: uncomment or add the actions you want.",
        "# Verbs: reply <thread> (indented body), resolve <thread>..., unresolve <thread>...,",
        "#        merge|ship [squash|merge|rebase] (must be last)",
    ]

    shown = threads if show_resolved else [t for t in threads if not t.is_resolved]
    for t in shown:
        loc = f"{t.path}:{t.line}" if t.path else ""
        status = "resolved" if t.is_resolved else "unresolved"
        out += ["", f"## thread {t.id}  {loc}  ({status})"]
        hunk = (t.comments[0].diff_hunk if t.comments else "").splitlines()
        if hunk:
            out.append("#   diff:")
            out += [f"#   | {line}" for line in hunk[-_HUNK_CONTEXT_LINES:]]
        for c in t.comments:
            out.append(f"#   {c.author}  {c.created_at}")
            out += _quote("#   > ", c.body)
        out += [f"# reply {t.id}", f"#   {_PLACEHOLDER_BODY}"]
        out.append(f"# {'unresolve' if t.is_resolved else 'resolve'} {t.id}")
    if not shown:
        out += ["", "## no unresolved review threads"]

    out += ["", "## merge", "# merge squash", "# ship squash"]
    return "\n".join(out) + "\n"


def parse(text: str) -> Plan:
    header: tuple[str, int, str] | None = None
    actions: list[Action] = []
    current: Action | None = None  # a `reply` still collecting its body
    body_lines: list[str] = []

    def finish_reply() -> None:
        nonlocal current, body_lines
        if current is None:
            return
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        current.body = "\n".join(body_lines)
        if not current.body or current.body.strip() == _PLACEHOLDER_BODY:
            raise PlanError(f"line {current.line}: reply {current.args[0]} has no body")
        actions.append(current)
        current, body_lines = None, []

    for n, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("#"):
            continue
        if not raw.strip():
            if current is not None:
                body_lines.append("")
            continue
        if current is not None and raw[0] in " \t":
            body_lines.append(raw[2:] if raw.startswith("  ") else raw.lstrip("\t "))
            continue
        finish_reply()

        if raw[0] in " \t":
            raise PlanError(f"line {n}: unexpected indented text outside a `reply` body")
        if raw.startswith("@pr"):
            m = _HEADER_RE.match(raw)
            if not m:
                raise PlanError(f"line {n}: malformed header, expected `@pr owner/name#N head=<sha>`")
            if header is not None:
                raise PlanError(f"line {n}: more than one @pr header")
            header = (m.group(1), int(m.group(2)), m.group(3))
            continue

        if header is None:
            raise PlanError(f"line {n}: action before the @pr header")
        if actions and actions[-1].kind in ("merge", "ship"):
            raise PlanError(f"line {n}: nothing may follow `{actions[-1].kind}` (line {actions[-1].line})")

        verb, *rest = raw.split()
        if verb == "reply":
            if len(rest) != 1:
                raise PlanError(f"line {n}: `reply` takes exactly one thread id")
            current = Action("reply", n, rest)
        elif verb in ("resolve", "unresolve"):
            if not rest:
                raise PlanError(f"line {n}: `{verb}` needs at least one thread id")
            actions.append(Action(verb, n, rest))
        elif verb in ("merge", "ship"):
            if len(rest) > 1 or (rest and rest[0] not in MERGE_METHODS):
                raise PlanError(f"line {n}: `{verb}` takes an optional method: {', '.join(MERGE_METHODS)}")
            actions.append(Action(verb, n, rest or ["squash"]))
        else:
            raise PlanError(f"line {n}: unknown action `{verb}`")
    finish_reply()

    if header is None:
        raise PlanError("missing `@pr owner/name#N head=<sha>` header")
    return Plan(repo=header[0], number=header[1], head=header[2], actions=actions)
