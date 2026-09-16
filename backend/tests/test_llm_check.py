"""Guards for the rollout provider smoke check; no test makes a paid call."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from app.config import Settings
from app.llm_check import main


def test_llm_check_refuses_invalid_configuration(monkeypatch, capsys) -> None:
    import app.llm_check as check

    monkeypatch.setattr(sys, "argv", ["app.llm_check"])
    monkeypatch.setattr(
        check,
        "get_settings",
        lambda: Settings(llm_provider="bogus", openai_api_key=""),
    )

    assert main() == 1
    assert "unsupported" in capsys.readouterr().err


def test_llm_check_refuses_a_blank_provider(monkeypatch, capsys) -> None:
    import app.llm_check as check

    monkeypatch.setattr(sys, "argv", ["app.llm_check"])
    monkeypatch.setattr(
        check, "get_settings", lambda: Settings(llm_provider="", openai_api_key="")
    )

    assert main() == 1
    assert "blank" in capsys.readouterr().err


def test_llm_check_reports_an_unavailable_provider(monkeypatch, capsys) -> None:
    import app.llm_check as check

    monkeypatch.setattr(sys, "argv", ["app.llm_check"])
    monkeypatch.setattr(
        check,
        "get_settings",
        lambda: Settings(llm_provider="openai", openai_api_key="key"),
    )
    monkeypatch.setattr(check, "build_evaluation_provider", lambda settings: None)

    assert main() == 1
    assert "unavailable" in capsys.readouterr().err


def test_llm_check_reports_provider_failure_without_secrets(
    monkeypatch, capsys
) -> None:
    import app.llm_check as check

    monkeypatch.setattr(sys, "argv", ["app.llm_check"])
    monkeypatch.setattr(
        check,
        "get_settings",
        lambda: Settings(llm_provider="openai", openai_api_key="secret-key-value"),
    )
    monkeypatch.setattr(
        check,
        "build_evaluation_provider",
        lambda settings: SimpleNamespace(provider_name="openai"),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(check, "evaluate_with_parse_retry", fail)

    assert main() == 1
    output = capsys.readouterr()
    assert "quota exceeded" in output.err
    assert "secret-key-value" not in output.err
