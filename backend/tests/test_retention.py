import json

from app.retention import retention_report


class _Rows:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def scalar_one(self):
        return self.value


class _Connection:
    def execute(self, statement, params=None):  # noqa: ANN001, ARG002
        sql = str(statement)
        if "pg_total_relation_size" in sql:
            return _Rows({"total_bytes": 1024 * 1024, "columns": 5})
        return _Rows(3)


def test_retention_report_is_dry_run_and_reports_bytes_and_counts() -> None:
    report = retention_report(_Connection())  # type: ignore[arg-type]

    assert report["dry_run"] is True
    assert report["table_sizes"]["jobs"]["total_mb"] == 1.0
    assert report["candidates"]["raw_listings_not_seen_since_cutoff"] == 3
    assert any("no rows are deleted" in note.lower() for note in report["notes"])
    json.dumps(report)
