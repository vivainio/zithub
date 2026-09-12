"""Shared test infrastructure: a fake for the `gh` subprocess boundary.

zithub.gh._run() and run_passthrough() are the only two seams every gh
invocation passes through, so patching subprocess.run here is enough to
test the whole CLI -- argparse -> cmd_* -> gh.* -> subprocess -- without
ever shelling out to a real gh binary.
"""

from __future__ import annotations

import subprocess

import pytest


class FakeCli:
    """Registers canned (stdout, returncode, stderr) responses keyed by the
    exact argv list a command would be invoked with, and stands in for
    subprocess.run. Registering the same args twice queues multiple
    responses, returned in order (for commands invoked more than once,
    e.g. a polling loop)."""

    def __init__(self) -> None:
        self._responses: dict[tuple[str, ...], list[tuple[str, int, str]]] = {}
        self.calls: list[list[str]] = []

    def set(self, args: list[str], stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self._responses.setdefault(tuple(args), []).append((stdout, returncode, stderr))

    def fail(self, args: list[str], stderr: str = "boom") -> None:
        self.set(args, returncode=1, stderr=stderr)

    def __call__(
        self,
        args: list[str],
        *,
        capture_output: bool = False,
        text: bool = True,
        check: bool = False,
        env=None,
    ) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        key = tuple(args)
        queue = self._responses.get(key)
        if not queue:
            registered = "\n".join(" ".join(k) for k in self._responses)
            raise AssertionError(
                f"unexpected command: {args!r}\nregistered commands:\n{registered}"
            )
        stdout, returncode, stderr = queue[0] if len(queue) == 1 else queue.pop(0)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    fake = FakeCli()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    """zithub.registry reads/writes under XDG_DATA_HOME -- point every test
    at an empty temp dir so none of them see (or pollute) this machine's
    real ~/.local/share/zithub/repos.jsonl."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
