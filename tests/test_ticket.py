from __future__ import annotations

from zithub import ticket


def test_github_issue_ref():
    result = ticket.check_ticket_reference("Fixes #123\n\nsome body text")
    assert result.found
    assert result.kind == "issue"
    assert result.detail == "#123"
    assert result.note is None


def test_bare_issue_ref():
    result = ticket.check_ticket_reference("Related to #45")
    assert result.found
    assert result.kind == "issue"
    assert result.detail == "#45"


def test_tracker_url():
    result = ticket.check_ticket_reference("See https://acme.atlassian.net/browse/ABC-123")
    assert result.found
    assert result.kind == "url"
    assert result.detail == "https://acme.atlassian.net/browse/ABC-123"
    assert result.note is None


def test_bare_jira_key_found_with_note():
    result = ticket.check_ticket_reference("Implements ABC-123")
    assert result.found
    assert result.kind == "jira-bare"
    assert result.detail == "ABC-123"
    assert result.note is not None
    assert "ABC-123" in result.note


def test_no_reference():
    result = ticket.check_ticket_reference("Just a plain description with no ticket")
    assert not result.found
    assert result.kind is None
    assert result.detail is None


def test_issue_ref_takes_priority_over_jira_key():
    result = ticket.check_ticket_reference("Fixes #7, related to ABC-123")
    assert result.kind == "issue"
    assert result.detail == "#7"


def test_url_takes_priority_over_bare_jira_key():
    result = ticket.check_ticket_reference("ABC-123 tracked at https://tracker.example/ABC-123")
    assert result.kind == "url"


def test_url_trailing_punctuation_trimmed():
    result = ticket.check_ticket_reference("(see https://acme.atlassian.net/browse/ABC-123).")
    assert result.detail == "https://acme.atlassian.net/browse/ABC-123"
