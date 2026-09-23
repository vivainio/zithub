"""Local sqlite cache of your open PRs, synced from GitHub by `zh board
sync` and read by `zh board` / `zh board query`.

A cache rather than a live view: `gh` round trips (one `gh pr view` per
open PR) are too slow to do on every `zh board` invocation, so sync is a
separate, explicit step, and stagnation ("no activity in N days") is
computed against whatever was last synced, not against live data.

Kept as an actual sqlite file (not zithub's usual JSONL registry format)
specifically so it can be queried directly — `zh board query "<SQL>"`,
or any other sqlite client pointed at the file — rather than only through
whatever views this module thinks to expose.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from . import gh


class BoardError(Exception):
    """Raised for bad input to this module (e.g. a non-SELECT query)."""


def _data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "zithub")


def db_path(host: str) -> str:
    """One db per GitHub site (github.com, a GHES host, ...), since "your
    open PRs" — and which login is "you" — differ per site."""
    return os.path.join(_data_dir(), "boards", f"{host}.sqlite")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    is_draft INTEGER NOT NULL,
    review_decision TEXT NOT NULL,
    ci_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    comment_count INTEGER NOT NULL,
    last_comment_at TEXT,
    last_comment_author TEXT,
    last_activity_at TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect(host: str) -> sqlite3.Connection:
    path = db_path(host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def sync(
    host: str, prs: list[gh.BoardPr], synced_at: str, login: str | None = None
) -> None:
    """Replace the table's contents with exactly `prs` — the scope is
    "your currently-open PRs", so a PR merged/closed since the last sync
    should simply disappear rather than linger as a stale row."""
    prune(host, {(p.repo, p.number) for p in prs})
    upsert(host, prs, synced_at)
    if login:
        set_login(host, login)


def cached_versions(host: str) -> dict[tuple[str, int], tuple[str, str]]:
    """(repo, number) -> (updated_at, ci_state) for every cached PR — what
    `zh board sync` compares against to skip PRs that haven't changed."""
    with _connect(host) as conn:
        return {
            (r["repo"], r["number"]): (r["updated_at"], r["ci_state"])
            for r in conn.execute("SELECT repo, number, updated_at, ci_state FROM prs")
        }


def prune(host: str, keep: set[tuple[str, int]]) -> None:
    """Drop every cached PR not in `keep` (i.e. merged/closed since the
    last sync)."""
    with _connect(host) as conn:
        stale = [
            (r["repo"], r["number"])
            for r in conn.execute("SELECT repo, number FROM prs")
            if (r["repo"], r["number"]) not in keep
        ]
        conn.executemany("DELETE FROM prs WHERE repo = ? AND number = ?", stale)


def upsert(host: str, prs: list[gh.BoardPr], synced_at: str) -> None:
    """Insert or refresh `prs` — committed per call, so a sync that fails
    partway keeps every batch it already fetched."""
    rows = [
        (
            p.repo,
            p.number,
            p.title,
            p.url,
            int(p.is_draft),
            p.review_decision,
            p.ci_state,
            p.created_at,
            p.updated_at,
            p.comment_count,
            p.last_comment_at,
            p.last_comment_author,
            max(p.updated_at, p.last_comment_at or ""),
            synced_at,
        )
        for p in prs
    ]
    with _connect(host) as conn:
        conn.executemany("INSERT OR REPLACE INTO prs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def set_login(host: str, login: str) -> None:
    """Stash your active gh login so `zh board focus` can tell "you last
    commented" from "someone else did" without its own gh call."""
    with _connect(host) as conn:
        conn.execute(
            "INSERT INTO meta VALUES ('login', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (login,),
        )


def get_login(host: str) -> str | None:
    with _connect(host) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'login'").fetchone()
        return row["value"] if row else None


@dataclass
class BoardRow:
    repo: str
    number: int
    title: str
    url: str
    is_draft: bool
    review_decision: str
    ci_state: str
    last_activity_at: str
    comment_count: int
    last_comment_author: str | None


def list_board(host: str) -> list[BoardRow]:
    """Every synced PR, oldest activity first — so a stagnated PR sorts to
    the top without needing a separate "stale" query."""
    with _connect(host) as conn:
        cur = conn.execute(
            "SELECT repo, number, title, url, is_draft, review_decision, ci_state, "
            "last_activity_at, comment_count, last_comment_author "
            "FROM prs ORDER BY last_activity_at ASC"
        )
        return [
            BoardRow(
                repo=r["repo"],
                number=r["number"],
                title=r["title"],
                url=r["url"],
                is_draft=bool(r["is_draft"]),
                review_decision=r["review_decision"],
                ci_state=r["ci_state"],
                last_activity_at=r["last_activity_at"],
                comment_count=r["comment_count"],
                last_comment_author=r["last_comment_author"],
            )
            for r in cur.fetchall()
        ]


def run_query(host: str, sql: str) -> tuple[list[str], list[tuple]]:
    """Runs a read-only `SELECT`/`WITH` query against the board db and
    returns (column names, rows). Anything else (INSERT/UPDATE/DELETE/etc,
    including via a stacked statement) is rejected — this is meant for ad
    hoc lookups against the synced snapshot, not for editing it."""
    stripped = sql.strip()
    if not stripped or not stripped.lstrip().upper().startswith(("SELECT", "WITH")):
        raise BoardError("only SELECT/WITH queries are allowed")
    with _connect(host) as conn:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(stripped)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, cur.fetchall()
