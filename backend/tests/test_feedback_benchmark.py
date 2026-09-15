from typing import Any

from app.feedback_benchmark import run_feedback_benchmark
from app.feedback_learning import SEMANTIC_ALPHA, SEMANTIC_BETA


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> "_Rows":
        return self

    def __iter__(self) -> Any:
        return iter(self.rows)


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def execute(self, *_args: Any, **_kwargs: Any) -> _Rows:
        return _Rows(self.rows)


def _row(rating: int, rank: int | None, lanes: list[str] | None = None) -> dict[str, Any]:
    return {
        "rating": rating,
        "applied": False,
        "rank": rank,
        "scoring_snapshot": {"candidate_lanes": lanes or []},
        "deterministic_result": {},
    }


def test_benchmark_reports_true_denominators_and_real_constants() -> None:
    connection = _Connection(
        [
            _row(5, 5, ["exploration", "learned_discovery"]),
            _row(1, 10),
            _row(4, 40),
            _row(2, 3),
        ]
    )

    result = run_feedback_benchmark(connection)  # type: ignore[arg-type]

    assert result["positive_labels"] == 2
    assert result["negative_labels"] == 2
    assert result["rated_in_top_30"] == 3
    assert result["labelled_recall_at_30"] == 0.5
    assert result["labelled_negative_share_at_30"] == round(2 / 3, 3)
    # Deprecated aliases mirror the correctly named metrics.
    assert result["recall_at_30"] == result["labelled_recall_at_30"]
    assert (
        result["false_positive_rate_at_30"] == result["labelled_negative_share_at_30"]
    )
    assert result["tuning_constants"]["semantic_alpha"] == SEMANTIC_ALPHA
    assert result["tuning_constants"]["semantic_beta"] == SEMANTIC_BETA
    assert result["exploration_hit_rate"] == 0.5
    assert result["learned_discovery_contribution"] == 0.5


def test_benchmark_handles_no_labels() -> None:
    result = run_feedback_benchmark(_Connection([]))  # type: ignore[arg-type]

    assert result["labelled_recall_at_30"] is None
    assert result["labelled_negative_share_at_30"] is None
