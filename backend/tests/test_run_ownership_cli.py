"""Manual CLI commands must respect database-owned run exclusion.

These are unit tests with no database: they verify that the manual matching
entry point asks for the shared profile publication lock and skips without
publishing when another owner holds it.
"""

from __future__ import annotations

import pytest

from app import match as match_module


class FakeOwnership:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def test_match_cli_skips_without_ownership(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(match_module, "acquire_publication_ownership", lambda: None)
    calls: list[int] = []
    monkeypatch.setattr(
        match_module,
        "run_matching",
        lambda *, max_jobs: calls.append(max_jobs) or {"recommended": 1},
    )
    monkeypatch.setattr("sys.argv", ["app.match", "--max-jobs", "5"])

    match_module.main()

    assert calls == []
    assert '"skipped": true' in capsys.readouterr().out


def test_match_cli_runs_with_ownership_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = FakeOwnership()
    monkeypatch.setattr(match_module, "acquire_publication_ownership", lambda: ownership)
    calls: list[int] = []
    monkeypatch.setattr(
        match_module,
        "run_matching",
        lambda *, max_jobs: calls.append(max_jobs) or {"recommended": 2},
    )
    monkeypatch.setattr("sys.argv", ["app.match", "--max-jobs", "7"])

    match_module.main()

    assert calls == [7]
    assert ownership.released is True


def test_match_cli_releases_ownership_when_matching_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = FakeOwnership()
    monkeypatch.setattr(match_module, "acquire_publication_ownership", lambda: ownership)

    def boom(*, max_jobs: int) -> dict[str, int]:
        raise RuntimeError("matching failed")

    monkeypatch.setattr(match_module, "run_matching", boom)
    monkeypatch.setattr("sys.argv", ["app.match"])

    with pytest.raises(RuntimeError, match="matching failed"):
        match_module.main()

    assert ownership.released is True
