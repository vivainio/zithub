"""Local registry of git checkouts zh has seen.

Populated as a side effect of ordinary `zh` usage (see
`cli._record_repo_seen`) and queried with `zh repos` — lets an AI agent
discover where a repo already lives on disk instead of guessing paths or
re-cloning. Ported from wazup's own registry, which this deliberately
mirrors rather than shares: same on-disk shape and behavior, kept as its
own database under zithub's own data dir.

Stored as an append-only JSONL log (one record per line) rather than a
single JSON document: `record_seen()` runs on nearly every `zh`
invocation, so it only appends a line — no read-modify-write of the whole
file — and a stat() call to decide whether the log has grown large enough
to be worth compacting. Later records for the same path supersede earlier
ones; readers (`list_seen`/`find_seen`) collapse the log down to the latest
record per path.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

# Compact (dedupe by path, drop stale paths) once the log outgrows this —
# a size check (stat, O(1)) rather than a line count, so the common-case
# write stays a single append with no full read.
_COMPACT_SIZE_BYTES = 64 * 1024


def _data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "zithub")


def _registry_path() -> str:
    return os.path.join(_data_dir(), "repos.jsonl")


def _read_records() -> list[dict]:
    try:
        with open(_registry_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # a partial last line from a crash mid-append — skip it
    return records


def _latest_by_path(records: list[dict]) -> dict[str, dict]:
    """Last-write-wins per path — later lines in the (append-only) log
    supersede earlier ones for the same path."""
    latest: dict[str, dict] = {}
    for r in records:
        path = r.get("path")
        if path:
            latest[path] = r
    return latest


def _append(record: dict) -> None:
    path = _registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _compact() -> None:
    """Rewrites the log with exactly one (still-existing) record per path —
    the only place a stale path is dropped from disk, since ordinary writes
    are append-only and never rewrite prior lines."""
    latest = {
        path: record
        for path, record in _latest_by_path(_read_records()).items()
        if os.path.isdir(path)
    }
    path = _registry_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for record in latest.values():
            f.write(json.dumps(record, sort_keys=True) + "\n")
    os.replace(tmp, path)


def record_seen(path: str, name_with_owner: str, branch: str) -> None:
    """Appends an upsert record for this checkout — O(1): no full read, no
    rewrite. The log is compacted only once it's grown past
    `_COMPACT_SIZE_BYTES`, so most calls cost a single append plus a stat()."""
    real = os.path.realpath(path)
    record = {"path": real, "repo": name_with_owner, "branch": branch, "last_seen": time.time()}
    _append(record)
    try:
        size = os.path.getsize(_registry_path())
    except OSError:
        size = 0
    if size > _COMPACT_SIZE_BYTES:
        _compact()


@dataclass
class RepoEntry:
    path: str
    repo: str
    branch: str
    last_seen: float


def list_seen() -> list[RepoEntry]:
    """All known checkouts, most-recently-seen first. Entries whose path no
    longer exists on disk are silently omitted."""
    latest = _latest_by_path(_read_records())
    entries = [
        RepoEntry(path=p, repo=e["repo"], branch=e.get("branch", "?"), last_seen=e["last_seen"])
        for p, e in latest.items()
        if os.path.isdir(p)
    ]
    entries.sort(key=lambda e: e.last_seen, reverse=True)
    return entries


def find_seen(query: str) -> list[RepoEntry]:
    """Checkouts whose repo name or path contains `query`, case-insensitive."""
    q = query.lower()
    return [e for e in list_seen() if q in e.repo.lower() or q in e.path.lower()]
