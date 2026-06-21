import json
from datetime import date

import pytest

from app.profile import load_profile_document, profile_json


def test_load_profile_document_reads_yaml(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("role_clusters:\n  - titles_fi:\n      - Ohjaaja\n", encoding="utf-8")

    assert load_profile_document(path) == {
        "role_clusters": [{"titles_fi": ["Ohjaaja"]}],
    }


def test_load_profile_document_reads_json(tmp_path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"location": {"home_city": "Oulu"}}), encoding="utf-8")

    assert load_profile_document(path) == {"location": {"home_city": "Oulu"}}


def test_load_profile_document_rejects_non_mapping(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("- invalid\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_profile_document(path)


def test_profile_json_serializes_dates() -> None:
    assert json.loads(profile_json({"updated": date(2026, 6, 20)})) == {
        "updated": "2026-06-20",
    }
