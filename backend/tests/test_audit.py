import json
from datetime import datetime, timezone

from app.audit import json_safe_latest_run


def test_json_safe_latest_run_serializes_row_mapping_values() -> None:
    row = {
        "id": 7,
        "status": "completed",
        "learned_version": 3,
        "started_at": datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc),
        "finished_at": None,
    }

    safe = json_safe_latest_run(row)

    assert safe == {
        "id": 7,
        "status": "completed",
        "learned_version": 3,
        "started_at": "2026-09-16T10:00:00+00:00",
        "finished_at": None,
    }
    assert json.loads(json.dumps(safe))["learned_version"] == 3


def test_json_safe_latest_run_handles_missing_row() -> None:
    assert json_safe_latest_run(None) is None
