"""Ticket-reference detection in PR titles/descriptions — pure logic, no
subprocess calls."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ISSUE_RE = re.compile(r"#\d+")
_URL_RE = re.compile(r"https?://\S+")
_JIRA_KEY_RE = re.compile(r"\b[A-Z]{2,10}-\d+\b")


@dataclass
class TicketCheck:
    found: bool
    kind: str | None = None  # "issue", "url", or "jira-bare"
    detail: str | None = None  # the matched text
    note: str | None = None  # a suggestion, shown even when found


def check_ticket_reference(text: str) -> TicketCheck:
    """Looks for a ticket/issue reference in `text` (typically a PR's title
    and body together), in priority order:

    1. A GitHub issue reference (`#123`, `Fixes #123`, `Closes #45`, ...) —
       matches gh's own issue-closing keywords, no config needed.
    2. Any URL — covers a Jira/Linear/etc. link, whatever the tracker.
    3. A bare Jira-style key (e.g. `ABC-123`) with no URL — still counts as
       a reference, but flagged with a suggestion to link it directly.
    """
    m = _ISSUE_RE.search(text)
    if m:
        return TicketCheck(found=True, kind="issue", detail=m.group(0))

    m = _URL_RE.search(text)
    if m:
        # trim trailing punctuation a URL picks up from surrounding prose or
        # markdown, e.g. "(see https://x.com/y)." or "https://x.com/y,"
        return TicketCheck(found=True, kind="url", detail=m.group(0).rstrip(").,;:'\""))

    m = _JIRA_KEY_RE.search(text)
    if m:
        return TicketCheck(
            found=True,
            kind="jira-bare",
            detail=m.group(0),
            note=f"'{m.group(0)}' is mentioned but isn't a link — consider linking directly to the ticket",
        )

    return TicketCheck(found=False)
